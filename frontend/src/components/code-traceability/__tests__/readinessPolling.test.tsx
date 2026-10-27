import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';
import { useBoardDeliveryReadiness } from '../useBoardDeliveryReadiness';
import { cards, persistedProjection } from './readinessFixture';

const api = vi.hoisted(() => ({ getPersistedReadinessProjection: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));

beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

function pending() {
  const value = persistedProjection();
  value.projection.freshness = 'pending_initial';
  value.projection.generation_id = null;
  value.summaries = [];
  return value;
}

it('polls a visible pending scope after 500 ms, pauses while hidden and resumes on visibility', async () => {
  let visible = true;
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visible ? 'visible' : 'hidden');
  api.getPersistedReadinessProjection.mockResolvedValueOnce(pending()).mockResolvedValue(persistedProjection());
  const view = renderHook(() => useBoardDeliveryReadiness('b', cards, true, '0'));
  await act(async () => { await Promise.resolve(); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  expect(view.result.current.c.persisted?.freshness).toBe('pending_initial');
  visible = false;
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  visible = true;
  await act(async () => { document.dispatchEvent(new Event('visibilitychange')); await Promise.resolve(); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
  expect(view.result.current.c.persisted?.generationId).toBe('generation-1');
});

it('polls a healthy delayed generation at 500 ms without overlapping a slow response', async () => {
  let resolve!: (value: ReturnType<typeof persistedProjection>) => void;
  api.getPersistedReadinessProjection.mockResolvedValueOnce(pending())
    .mockImplementation(() => new Promise(r => { resolve = r; }));
  const view = renderHook(() => useBoardDeliveryReadiness('b', cards, true, '0'));
  await act(async () => { await Promise.resolve(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(499); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
  await act(async () => resolve(persistedProjection()));
  expect(view.result.current.c.persisted?.freshness).toBe('current');
});

it('rotates four pending Specs through three polling slots', async () => {
  const manyCards = Array.from({ length: 4 }, (_, index) => ({ ...cards[0], id: `card-${index}`, spec_id: `spec-${index}` }));
  api.getPersistedReadinessProjection.mockImplementation((_board, specId) => {
    const value = pending();
    value.scope.spec_id = specId;
    return Promise.resolve(value);
  });
  renderHook(() => useBoardDeliveryReadiness('b', manyCards, true, '0'));
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(4);
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(api.getPersistedReadinessProjection.mock.calls.filter(([, specId]) => specId === 'spec-3').length).toBeGreaterThan(1);
});

it('retries transient failures at 1, 2 and 5 seconds without overlapping a slow request', async () => {
  api.getPersistedReadinessProjection.mockRejectedValue(new TypeError('offline'));
  const view = renderHook(() => useBoardDeliveryReadiness('b', cards, true, '0'));
  await act(async () => { await Promise.resolve(); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(999); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(3);
  let resolve!: (value: ReturnType<typeof persistedProjection>) => void;
  api.getPersistedReadinessProjection.mockImplementation(() => new Promise(r => { resolve = r; }));
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(4);
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(4);
  await act(async () => resolve(persistedProjection()));
  expect(view.result.current.c.persisted?.freshness).toBe('current');
});
