import { renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useDashboardApi } from '../api';

vi.mock('@/contexts/ApiContext', async () => {
  const { AuthenticatedFetch } = await import('@/lib/authFetch');
  const client = new AuthenticatedFetch(async () => null, '/api/v1');
  return { useApiClient: () => client };
});

describe('Board deletion HTTP outcomes', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('accepts a real 204 without attempting to parse an empty body', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetch);
    const { result } = renderHook(() => useDashboardApi());
    await expect(result.current.deleteBoard('e2e')).resolves.toBeUndefined();
    expect(fetch).toHaveBeenCalledWith('/api/v1/boards/e2e', expect.objectContaining({ method: 'DELETE' }));
  });

  it.each([403, 409, 500])('propagates HTTP %i instead of reporting successful deletion', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: {
      code: 'board_erasure_busy', message: 'Wait for the active operation.', retryable: true,
    } }), { status, headers: { 'Content-Type': 'application/json' } })));
    const { result } = renderHook(() => useDashboardApi());
    await expect(result.current.deleteBoard('e2e')).rejects.toMatchObject({
      name: 'AuthenticatedFetchError', status, message: 'Wait for the active operation.',
    });
  });

  it('does not swallow a network failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const { result } = renderHook(() => useDashboardApi());
    await expect(result.current.deleteBoard('e2e')).rejects.toThrow('Failed to fetch');
  });
});
