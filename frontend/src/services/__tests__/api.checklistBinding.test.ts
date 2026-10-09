import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { useDashboardApi } from '../api';

const client = vi.hoisted(() => ({ fetchJson: vi.fn(), fetch: vi.fn() }));
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => client }));

describe('checklist binding initial configuration', () => {
  it('recognizes only the explicit missing-binding response', async () => {
    client.fetchJson.mockRejectedValue(new AuthenticatedFetchError({
      status: 422, message: 'validation_failed',
      details: { reason_code: 'checklist_board_binding_missing' },
    }));
    const { result } = renderHook(() => useDashboardApi());
    await expect(result.current.getChecklistBinding('board-1')).resolves.toBeNull();
  });

  it.each([403, 404, 500])('preserves a %s read failure', async (status) => {
    const error = new AuthenticatedFetchError({ status, message: 'unavailable' });
    client.fetchJson.mockRejectedValue(error);
    const { result } = renderHook(() => useDashboardApi());
    await expect(result.current.getChecklistBinding('board-1')).rejects.toBe(error);
  });
});
