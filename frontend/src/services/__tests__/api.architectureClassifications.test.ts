import { renderHook } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { useDashboardApi } from '../api';
import type { ArchitectureClassificationBatch } from '@/types/architecture-classifications';
const client = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));
beforeEach(() => client.fetchJson.mockReset());

it('posts the exact authored batch without hidden automatic retries or per-IR writes', async () => {
  const batch: ArchitectureClassificationBatch = { expected_spec_version: 9, expected_spec_edition: 2, idempotency_key: 'same-intent', decisions: [{ candidate_ref: 'candidate', expected_source_digest: 'a'.repeat(64), disposition: 'context_only', scope_paths: [''], reason: 'Outside this delivery' }] };
  const body = { replayed: true }; client.fetchJson.mockResolvedValue(body);
  const { result } = renderHook(() => useDashboardApi());
  expect(await result.current.classifyArchitectureCandidates('board/a', 'spec b', batch)).toBe(body);
  expect(client.fetchJson).toHaveBeenCalledExactlyOnceWith('/boards/board%2Fa/specs/spec%20b/architecture-classifications', { method: 'POST', body: JSON.stringify(batch), maxRetries: 0 });
});

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
