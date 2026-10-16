import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { useBoardDeliveryReadiness } from '../useBoardDeliveryReadiness';
import { useDeliveryReadiness } from '../useDeliveryReadiness';
import { boardSummary, cards, readiness } from './readinessFixture';

const api = vi.hoisted(() => ({ getDeliveryReadinessSummary: vi.fn(), getDeliveryReadiness: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
beforeEach(() => { vi.clearAllMocks(); api.getDeliveryReadinessSummary.mockResolvedValue(boardSummary()); });
afterEach(cleanup);

it('reuses one summary per Spec and clears old percentages while refreshing related cards', async () => {
  const view = renderHook(() => useBoardDeliveryReadiness('b', cards, true, '0'));
  await waitFor(() => expect(view.result.current.c.value?.verification.percent).toBe(75));
  expect(api.getDeliveryReadinessSummary).toHaveBeenCalledTimes(1);
  const next = boardSummary(); next.cards.forEach(row => { row.delivery_readiness.verification.percent = 50; });
  let resolve!: (value: typeof next) => void;
  api.getDeliveryReadinessSummary.mockImplementation(() => new Promise(r => { resolve = r; }));
  act(() => window.dispatchEvent(new Event('pulse:delivery-evidence-changed')));
  await waitFor(() => expect(view.result.current.c).toEqual({ loading: true }));
  await act(async () => resolve(next));
  await waitFor(() => expect(view.result.current.other.value?.verification.percent).toBe(50));
  expect(cards[0].status).toBe('in_progress');
});

it('does not read without permission; denial removes previous values and disabling clears context', async () => {
  const view = renderHook(({ enabled }) => useBoardDeliveryReadiness('b', cards, enabled, '0'), { initialProps: { enabled: false } });
  expect(api.getDeliveryReadinessSummary).not.toHaveBeenCalled();
  view.rerender({ enabled: true });
  await waitFor(() => expect(view.result.current.c.value).toBeDefined());
  api.getDeliveryReadinessSummary.mockRejectedValue(new Error('Denied'));
  act(() => window.dispatchEvent(new Event('focus')));
  await waitFor(() => expect(view.result.current.c).toEqual({ error: 'Denied' }));
  view.rerender({ enabled: false }); expect(view.result.current).toEqual({});
});

it.each(['board', 'card', 'snapshot', 'duplicate', 'version'])('rejects %s mismatches explicitly', async kind => {
  const data = boardSummary();
  if (kind === 'board') data.delivery_readiness.scope.board_id = 'foreign';
  if (kind === 'card') data.cards[0].delivery_readiness.scope.card_id = 'foreign';
  if (kind === 'snapshot') data.cards[0].delivery_readiness.snapshot_id = 'b'.repeat(64);
  if (kind === 'duplicate') data.cards.push(data.cards[0]);
  if (kind === 'version') data.metrics_version = 1;
  api.getDeliveryReadinessSummary.mockResolvedValue(data);
  const view = renderHook(() => useBoardDeliveryReadiness('b', cards, true, '0'));
  await waitFor(() => expect(view.result.current.c.error).toBeTruthy());
  expect(view.result.current.c.value).toBeUndefined();
});

it('U08: a late previous Card response cannot replace the current scope; unmount aborts', async () => {
  let resolveOld!: (value: ReturnType<typeof readiness>) => void;
  api.getDeliveryReadiness.mockImplementation((_board, _spec, id) => id === 'c'
    ? new Promise(r => { resolveOld = r; }) : Promise.resolve(readiness(id)));
  const view = renderHook(({ id }) => useDeliveryReadiness('b', 's', id), { initialProps: { id: 'c' } });
  const oldSignal = api.getDeliveryReadiness.mock.calls[0][3];
  view.rerender({ id: 'other' });
  await waitFor(() => expect(view.result.current.value?.scope.card_id).toBe('other'));
  expect(oldSignal.aborted).toBe(true);
  await act(async () => resolveOld(readiness('c')));
  expect(view.result.current.value?.scope.card_id).toBe('other');
  const currentSignal = api.getDeliveryReadiness.mock.calls[1][3];
  view.unmount(); expect(currentSignal.aborted).toBe(true);
});

it('U08: a late board summary cannot overwrite a newly selected Board', async () => {
  let resolveOld!: (value: ReturnType<typeof boardSummary>) => void;
  api.getDeliveryReadinessSummary.mockImplementation((board) => board === 'b'
    ? new Promise(r => { resolveOld = r; }) : Promise.reject(new Error('New board unavailable')));
  const view = renderHook(({ board }) => useBoardDeliveryReadiness(board, cards, true, '0'), { initialProps: { board: 'b' } });
  const signal = api.getDeliveryReadinessSummary.mock.calls[0][2];
  view.rerender({ board: 'new' });
  await waitFor(() => expect(view.result.current.c.error).toBe('New board unavailable'));
  await act(async () => resolveOld(boardSummary()));
  expect(view.result.current.c.error).toBe('New board unavailable');
  expect(signal.aborted).toBe(true);
});
