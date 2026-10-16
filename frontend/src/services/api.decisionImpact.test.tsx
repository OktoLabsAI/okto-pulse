import { renderHook } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
const client = vi.hoisted(() => ({ fetchJson: vi.fn().mockResolvedValue({}) }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
import { useDashboardApi } from './api';

it('encodes domain IDs and sends bounded depth, cursor and cancellation', async () => {
  const { result } = renderHook(() => useDashboardApi());
  const signal = new AbortController().signal;
  await result.current.getDecisionImpact('board/one', 'spec/two', 'decision/three', { limit: 20, max_depth: 5, cursor: 'opaque:/x' }, signal);
  const [raw, options] = client.fetchJson.mock.calls[0];
  const url = new URL(raw, 'http://local');
  expect(url.pathname).toBe('/boards/board%2Fone/specs/spec%2Ftwo/decisions/decision%2Fthree/impact');
  expect(Object.fromEntries(url.searchParams)).toEqual({ limit: '20', max_depth: '5', cursor: 'opaque:/x' });
  expect(options.signal).toBe(signal);
});
