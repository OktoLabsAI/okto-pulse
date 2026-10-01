import { renderHook } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
const client = vi.hoisted(() => ({ fetchJson: vi.fn().mockResolvedValue({}) }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
import { useDashboardApi } from './api';

it('encodes the Board, preserves the pinned query and forwards cancellation', async () => {
  const { result } = renderHook(() => useDashboardApi());
  const signal = new AbortController().signal;
  await result.current.getBugClusters('board/one', { from: '2026-09-01T00:00:00Z',
    to: '2026-09-16T00:00:00Z', group_by: 'learning', status: 'done', severity: 'major',
    limit: 2, cursor: 'opaque:abc/123' }, signal);
  const [raw, options] = client.fetchJson.mock.calls[0];
  const url = new URL(raw, 'http://local');
  expect(url.pathname).toBe('/boards/board%2Fone/analytics/bug-clusters');
  expect(Object.fromEntries(url.searchParams)).toEqual({ from: '2026-09-01T00:00:00Z',
    to: '2026-09-16T00:00:00Z', group_by: 'learning', status: 'done', severity: 'major',
    limit: '2', cursor: 'opaque:abc/123' });
  expect(options.signal).toBe(signal);
});
