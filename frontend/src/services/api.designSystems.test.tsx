import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchJson = vi.hoisted(() => vi.fn());

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => ({ fetchJson }),
}));

import { useDashboardApi } from './api';

describe('Design System API projections', () => {
  beforeEach(() => {
    fetchJson.mockReset();
  });

  it('drains paginated summary envelopes while preserving the UI array contract', async () => {
    fetchJson
      .mockResolvedValueOnce({
        items: [{ id: 'ds-a', title: 'A', payload_available: true }],
        count: 1,
        next_cursor: 'cursor-2',
        profile: 'summary',
      })
      .mockResolvedValueOnce({
        items: [{ id: 'ds-b', title: 'B', payload_available: false }],
        count: 1,
        next_cursor: null,
        profile: 'summary',
      });
    const { result } = renderHook(() => useDashboardApi());

    const items = await result.current.listDesignSystems('inline', 'board-1');

    expect(items.map((item) => item.id)).toEqual(['ds-a', 'ds-b']);
    expect(fetchJson).toHaveBeenCalledTimes(2);
    const firstUrl = new URL(fetchJson.mock.calls[0][0], 'http://local');
    expect(Object.fromEntries(firstUrl.searchParams)).toEqual({
      scope: 'inline',
      profile: 'summary',
      limit: '100',
      board_id: 'board-1',
    });
    const secondUrl = new URL(fetchJson.mock.calls[1][0], 'http://local');
    expect(secondUrl.searchParams.get('cursor')).toBe('cursor-2');
  });

  it('propagates the requested detail/full profile on item reads', async () => {
    fetchJson.mockResolvedValue({ id: 'ds-a', payload: { tokens: {} } });
    const { result } = renderHook(() => useDashboardApi());

    await result.current.getDesignSystem('ds-a', 'detail', 'board-1');

    const url = new URL(fetchJson.mock.calls[0][0], 'http://local');
    expect(url.pathname).toBe('/design-systems/ds-a');
    expect(url.searchParams.get('profile')).toBe('detail');
    expect(url.searchParams.get('board_id')).toBe('board-1');
  });
});
