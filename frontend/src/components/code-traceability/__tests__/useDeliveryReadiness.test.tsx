import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { useBoardDeliveryReadiness } from '../useBoardDeliveryReadiness';
import { useDeliveryReadiness } from '../useDeliveryReadiness';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { cards, persistedProjection, readiness } from './readinessFixture';

const api = vi.hoisted(() => ({ getPersistedReadinessProjection: vi.fn(), getDeliveryReadiness: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
beforeEach(() => { vi.clearAllMocks(); api.getPersistedReadinessProjection.mockResolvedValue(persistedProjection()); });
afterEach(cleanup);

it('reuses one summary per Spec and retains the previous generation while refreshing related cards', async () => {
  const view = renderHook(() => useBoardDeliveryReadiness('b', cards, true, '0'));
  await waitFor(() => expect(view.result.current.c.persisted?.summary?.verification.percent).toBe(75));
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  const next = persistedProjection(); next.summaries.forEach(row => { row.verification.percent = 50; });
  let resolve!: (value: typeof next) => void;
  api.getPersistedReadinessProjection.mockImplementation(() => new Promise(r => { resolve = r; }));
  act(() => window.dispatchEvent(new Event('pulse:delivery-evidence-changed')));
  await waitFor(() => expect(view.result.current.c.persisted?.freshness).toBe('updating'));
  expect(view.result.current.c.persisted?.summary?.verification.percent).toBe(75);
  expect(view.result.current.other.persisted?.generationId).toBe('generation-1');
  await act(async () => resolve(next));
  await waitFor(() => expect(view.result.current.other.persisted?.summary?.verification.percent).toBe(50));
  expect(cards[0].status).toBe('in_progress');
});

it('does not read without permission; denial removes previous values and disabling clears context', async () => {
  const view = renderHook(({ enabled }) => useBoardDeliveryReadiness('b', cards, enabled, '0'), { initialProps: { enabled: false } });
  expect(api.getPersistedReadinessProjection).not.toHaveBeenCalled();
  view.rerender({ enabled: true });
  await waitFor(() => expect(view.result.current.c.persisted).toBeDefined());
  api.getPersistedReadinessProjection.mockRejectedValue(new AuthenticatedFetchError({ status: 403, message: 'Denied' }));
  act(() => window.dispatchEvent(new Event('focus')));
  await waitFor(() => expect(view.result.current.c).toEqual({ error: 'Denied' }));
  view.rerender({ enabled: false }); expect(view.result.current).toEqual({});
});

it('keeps the Spec projection during a same-scope refresh, then replaces the whole generation', async () => {
  const view = renderHook(({ revision }) => useDeliveryReadiness('b', 's', undefined, revision, true, 'projection'),
    { initialProps: { revision: 0 } });
  await waitFor(() => expect(view.result.current.persisted?.generationId).toBe('generation-1'));
  const next = persistedProjection();
  next.projection.generation_id = 'generation-2';
  next.summaries.forEach(row => { row.verification.percent = 50; });
  let resolve!: (value: typeof next) => void;
  api.getPersistedReadinessProjection.mockImplementation(() => new Promise(r => { resolve = r; }));
  view.rerender({ revision: 1 });
  await waitFor(() => expect(view.result.current.persisted?.freshness).toBe('updating'));
  expect(view.result.current.persisted?.generationId).toBe('generation-1');
  expect(view.result.current.persisted?.summary?.verification.percent).toBe(75);
  await act(async () => resolve(next));
  await waitFor(() => expect(view.result.current.persisted?.generationId).toBe('generation-2'));
  expect(view.result.current.persisted?.summary?.verification.percent).toBe(50);
});

it.each(['board', 'card', 'generation', 'duplicate', 'version'])('rejects %s mismatches explicitly', async kind => {
  const data = persistedProjection();
  if (kind === 'board') data.scope.board_id = 'foreign';
  if (kind === 'card') data.summaries[1].card_id = 'foreign';
  if (kind === 'generation') data.projection.generation_id = null;
  if (kind === 'duplicate') data.summaries.push(data.summaries[1]);
  if (kind === 'version') (data.projection as { projection_version: number }).projection_version = 2;
  api.getPersistedReadinessProjection.mockResolvedValue(data);
  const view = renderHook(() => useBoardDeliveryReadiness('b', cards, true, '0'));
  await waitFor(() => expect(view.result.current.c.error).toBeTruthy());
  expect(view.result.current.c.persisted).toBeUndefined();
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
  let resolveOld!: (value: ReturnType<typeof persistedProjection>) => void;
  api.getPersistedReadinessProjection.mockImplementation((board) => board === 'b'
    ? new Promise(r => { resolveOld = r; }) : Promise.reject(new Error('New board unavailable')));
  const view = renderHook(({ board }) => useBoardDeliveryReadiness(board, cards, true, '0'), { initialProps: { board: 'b' } });
  const signal = api.getPersistedReadinessProjection.mock.calls[0][3];
  view.rerender({ board: 'new' });
  await waitFor(() => expect(view.result.current.c.error).toBe('New board unavailable'));
  await act(async () => resolveOld(persistedProjection()));
  expect(view.result.current.c.error).toBe('New board unavailable');
  expect(signal.aborted).toBe(true);
});
