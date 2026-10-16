import { renderHook } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
const client = vi.hoisted(() => ({ fetchJson: vi.fn().mockResolvedValue({}) }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
import { useDashboardApi } from './api';

it('encodes both scope IDs and forwards cursor and cancellation', async () => {
  const { result } = renderHook(() => useDashboardApi());
  const signal = new AbortController().signal;
  await result.current.getSpecCoverage('board/one', 'spec/two', { limit: 20, cursor: 'opaque:/x' }, signal);
  const [raw, options] = client.fetchJson.mock.calls[0];
  const url = new URL(raw, 'http://local');
  expect(url.pathname).toBe('/boards/board%2Fone/specs/spec%2Ftwo/coverage');
  expect(Object.fromEntries(url.searchParams)).toEqual({ limit: '20', cursor: 'opaque:/x' });
  expect(options.signal).toBe(signal);
});
