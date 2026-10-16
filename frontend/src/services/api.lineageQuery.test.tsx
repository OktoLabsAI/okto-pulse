import { renderHook } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
const client = vi.hoisted(() => ({ fetchJson: vi.fn().mockResolvedValue({}) }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
import { useDashboardApi } from './api';

it('encodes the Board and canonical subject and forwards bounds/cancellation', async () => {
  const { result } = renderHook(() => useDashboardApi());
  const signal = new AbortController().signal;
  await result.current.getSourceLineage('board/one', { subject_ref: 'amendment_hotfix_revision:a', max_depth: 6, cursor: 'opaque:/x' }, signal);
  const [raw, options] = client.fetchJson.mock.calls[0];
  const url = new URL(raw, 'http://local');
  expect(url.pathname).toBe('/boards/board%2Fone/lineage');
  expect(Object.fromEntries(url.searchParams)).toEqual({ subject_ref: 'amendment_hotfix_revision:a', max_depth: '6', cursor: 'opaque:/x' });
  expect(options.signal).toBe(signal);
});
