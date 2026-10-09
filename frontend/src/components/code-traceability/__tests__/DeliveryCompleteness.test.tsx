import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react';
import { DeliveryCompletenessView } from '../DeliveryCompletenessView';
import { useTaskDeliveryCompleteness } from '../useTaskDeliveryCompleteness';
import { KanbanCard } from '@/components/kanban/KanbanCard';
import { CardDeliveryDoDPanel } from '../CardDeliveryDoDPanel';
import type { CardSummary } from '@/types';

const api = vi.hoisted(() => ({ getDeliveryEvidence: vi.fn(), getBoard: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('@dnd-kit/sortable', () => ({ useSortable: () => ({ setNodeRef: vi.fn(), attributes: {}, listeners: {} }) }));
const score = { percent: 45, planned: 1, implemented: 2, verified: 1, accepted: 0, total: 4, scope_sha256: 'a'.repeat(64), reason: null };
const task: CardSummary = { id: 'c', board_id: 'b', spec_id: 's', card_type: 'normal', title: 'Implement capacity', status: 'in_progress', labels: [], created_at: '2026-10-09', updated_at: '2026-10-09', description: null, priority: 'none', position: 0, assignee_id: null, created_by: 'agent', due_date: null, test_scenario_ids: null, conclusions: [], validations: [] };
const cards: CardSummary[] = [task, { ...task, id: 'other' }];
const projection = () => ({ board_id: 'b', spec_id: 's', edition: 1, candidates: [], implementations: [], rows: [], tests: [], rejected_record_ids: [],
  per_card: [{ card_id: 'c', status: 'in_progress', obligations: [], delivery_completeness: score }, { card_id: 'other', delivery_completeness: score }] });
beforeEach(() => { vi.clearAllMocks(); api.getDeliveryEvidence.mockResolvedValue(projection()); api.getBoard.mockResolvedValue({ settings: {} }); });
afterEach(cleanup);

it('renders the same server-calculated percentage on the task cover and Delivery bar', async () => {
  render(<><KanbanCard card={cards[0]} nameMap={{}} onClick={vi.fn()} deliveryCompleteness={{ value: score }} /><CardDeliveryDoDPanel boardId="b" card={{ id: 'c', card_type: 'normal', spec_id: 's' }} /></>);
  expect(screen.getByText('45%')).toBeVisible();
  expect(screen.getByRole('progressbar', { name: 'Task delivery progress' })).toHaveAttribute('aria-valuenow', '45');
  expect(await screen.findByRole('progressbar', { name: 'Delivery progress' })).toHaveAttribute('aria-valuenow', '45');
  expect(screen.getByText(/1 planned · 2 implemented · 1 verified · 0 accepted/)).toBeVisible();
  expect(screen.getByText(/Gates remain independent/)).toBeVisible();
});

it.each(['scope_missing', 'scope_incomplete'] as const)('never presents %s as zero percent', reason => {
  render(<DeliveryCompletenessView compact value={{ ...score, percent: null, total: 0, reason }} />);
  expect(screen.getByText('Not calculable')).toBeVisible();
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});

it('does not trust malformed or unavailable percentages', () => {
  render(<DeliveryCompletenessView value={{ ...score, percent: 100 }} />);
  expect(screen.getByText('Unavailable')).toBeVisible();
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});

it('shares the Spec read and refreshes declining evidence without changing task status', async () => {
  const { result } = renderHook(() => useTaskDeliveryCompleteness('b', cards, true, '0'));
  await waitFor(() => expect(result.current.c.value?.percent).toBe(45));
  expect(api.getDeliveryEvidence).toHaveBeenCalledTimes(1);
  const next = projection(); next.per_card[0].delivery_completeness = { ...score, percent: 25, planned: 2, implemented: 2, verified: 0 };
  api.getDeliveryEvidence.mockResolvedValue(next);
  act(() => window.dispatchEvent(new Event('pulse:delivery-evidence-changed')));
  await waitFor(() => expect(result.current.c.value?.percent).toBe(25));
  expect(cards[0].status).toBe('in_progress');
});

it('does not fetch without permission and clears scores on read failure', async () => {
  const { result, rerender } = renderHook(({ enabled }) => useTaskDeliveryCompleteness('b', cards, enabled, '0'), { initialProps: { enabled: false } });
  expect(api.getDeliveryEvidence).not.toHaveBeenCalled();
  rerender({ enabled: true });
  await waitFor(() => expect(result.current.c.value?.percent).toBe(45));
  api.getDeliveryEvidence.mockRejectedValue(new Error('Denied'));
  act(() => window.dispatchEvent(new Event('focus')));
  await waitFor(() => expect(result.current.c).toEqual({}));
  rerender({ enabled: false });
  expect(result.current).toEqual({});
});

it('rejects a foreign scope and aborts pending reads on unmount', async () => {
  api.getDeliveryEvidence.mockResolvedValue({ ...projection(), board_id: 'foreign' });
  const view = renderHook(() => useTaskDeliveryCompleteness('b', cards, true, '0'));
  await waitFor(() => expect(view.result.current.c).toEqual({}));
  api.getDeliveryEvidence.mockImplementation(() => new Promise(() => {}));
  act(() => window.dispatchEvent(new Event('focus')));
  await waitFor(() => expect(api.getDeliveryEvidence).toHaveBeenCalledTimes(2));
  const signal = api.getDeliveryEvidence.mock.calls[1][2];
  view.unmount(); expect(signal.aborted).toBe(true);
});
