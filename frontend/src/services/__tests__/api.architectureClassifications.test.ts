import { renderHook } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { useDashboardApi } from '../api';
const client = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
beforeEach(() => client.fetchJson.mockReset());

it('encodes scope, pagination, filters and cancellation without sending a write', async () => {
  const body = { total: null }; client.fetchJson.mockResolvedValue(body);
  const { result } = renderHook(() => useDashboardApi());
  const controller = new AbortController();
  expect(await result.current.getArchitectureClassifications('board/a', 'spec b', controller.signal, { offset: 25, limit: 25, state: 'review_required' })).toBe(body);
  const [path, init] = client.fetchJson.mock.calls[0];
  const url = new URL(path, 'https://pulse.invalid');
  expect(url.pathname).toBe('/boards/board%2Fa/specs/spec%20b/architecture-classifications');
  expect(Object.fromEntries(url.searchParams)).toEqual({ offset: '25', limit: '25', state: 'review_required' });
  expect(init).toEqual({ signal: controller.signal });
});

it('preserves exact candidate and source digest for historical detail', async () => {
  const { result } = renderHook(() => useDashboardApi());
  await result.current.getArchitectureClassifications('board', 'spec', undefined, { candidateId: 'a/b & c', sourceDigest: 'd'.repeat(64) });
  const url = new URL(client.fetchJson.mock.calls[0][0], 'https://pulse.invalid');
  expect(Object.fromEntries(url.searchParams)).toEqual({ candidate_id: 'a/b & c', source_digest: 'd'.repeat(64) });
});
