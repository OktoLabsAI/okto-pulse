import { renderHook } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { useDashboardApi } from '../api';

const client = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
beforeEach(() => client.fetchJson.mockReset());

it('encodes scope, propagates cancellation, and only reads candidates', async () => {
  const resultBody = { source_complete: false, total: null };
  client.fetchJson.mockResolvedValue(resultBody);
  const controller = new AbortController();
  const { result } = renderHook(() => useDashboardApi());
  expect(await result.current.getArchitectureCandidates('board/a', 'spec b', controller.signal)).toBe(resultBody);
  expect(client.fetchJson).toHaveBeenCalledExactlyOnceWith(
    '/boards/board%2Fa/specs/spec%20b/architecture-candidates', { signal: controller.signal },
  );
});

it('preserves the requested page without requesting full contracts', async () => {
  client.fetchJson.mockResolvedValue({ offset: 25, limit: 25, candidates: [] });
  const controller = new AbortController();
  const { result } = renderHook(() => useDashboardApi());
  await result.current.getArchitectureCandidates('board', 'spec', controller.signal, { offset: 25, limit: 25 });
  const [path, options] = client.fetchJson.mock.calls[0];
  const url = new URL(path, 'https://pulse.invalid');
  expect(url.pathname).toBe('/boards/board/specs/spec/architecture-candidates');
  expect(Object.fromEntries(url.searchParams)).toEqual({ offset: '25', limit: '25' });
  expect(options).toEqual({ signal: controller.signal });
});

it('preserves the exact candidate identity and source digest for detail reads', async () => {
  client.fetchJson.mockResolvedValue({ candidates: [] });
  const { result } = renderHook(() => useDashboardApi());
  await result.current.getArchitectureCandidates('board', 'spec', undefined, {
    candidateId: 'candidate/a & b', sourceDigest: 'd'.repeat(64),
  });
  const url = new URL(client.fetchJson.mock.calls[0][0], 'https://pulse.invalid');
  expect(url.pathname).toBe('/boards/board/specs/spec/architecture-candidates');
  expect(Object.fromEntries(url.searchParams)).toEqual({ candidate_id: 'candidate/a & b', source_digest: 'd'.repeat(64) });
});
