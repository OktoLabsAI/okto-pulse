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
