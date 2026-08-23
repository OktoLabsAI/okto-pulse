import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiClient = vi.hoisted(() => ({
  fetchJson: vi.fn(),
  fetch: vi.fn(),
}));

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => apiClient,
}));

import { useDashboardApi } from './api';

describe('Board KG effectiveness API client', () => {
  beforeEach(() => {
    apiClient.fetchJson.mockReset();
    apiClient.fetch.mockReset();
    apiClient.fetchJson.mockResolvedValue({});
  });

  it('serializes repeated canonical filters with an opaque cursor and limit', async () => {
    const { result } = renderHook(() => useDashboardApi());

    await result.current.getBoardKgAnalytics('board/1', '2026-08-01', '2026-08-21', {
      cognitiveStatus: ['pending', 'failed', 'pending'],
      artifactTypes: ['spec', ' card ', 'spec'],
      cursor: 'offset:100/opaque',
      limit: 100,
    });

    const rawUrl = apiClient.fetchJson.mock.calls[0][0] as string;
    const url = new URL(rawUrl, 'http://local');
    expect(url.pathname).toBe('/boards/board/1/analytics/kg-effectiveness');
    expect(url.searchParams.get('from')).toBe('2026-08-01');
    expect(url.searchParams.get('to')).toBe('2026-08-21');
    expect(url.searchParams.getAll('cognitive_status')).toEqual(['failed', 'pending']);
    expect(url.searchParams.getAll('artifact_type')).toEqual(['card', 'spec']);
    expect(url.searchParams.get('cursor')).toBe('offset:100/opaque');
    expect(url.searchParams.get('limit')).toBe('100');
    expect(url.searchParams.has('as_of')).toBe(false);
  });

  it('uses the same server filters for the CSV projection', async () => {
    const originalCreateObjectUrl = URL.createObjectURL;
    const originalRevokeObjectUrl = URL.revokeObjectURL;
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:kg-export') });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    apiClient.fetch.mockResolvedValue(new Response('csv', { status: 200 }));
    const { result } = renderHook(() => useDashboardApi());

    try {
      await result.current.exportBoardKgAnalyticsCsv('board-1', '2026-08-01', '2026-08-21', {
        cognitiveStatus: ['consolidated'],
        artifactTypes: ['spec'],
        cursor: null,
        limit: 500,
      });

      const rawUrl = apiClient.fetch.mock.calls[0][0] as string;
      const url = new URL(rawUrl, 'http://local');
      expect(url.pathname).toBe('/boards/board-1/analytics/kg-effectiveness/export');
      expect(url.searchParams.getAll('cognitive_status')).toEqual(['consolidated']);
      expect(url.searchParams.getAll('artifact_type')).toEqual(['spec']);
      expect(url.searchParams.get('limit')).toBe('500');
      expect(url.searchParams.has('cursor')).toBe(false);
      expect(click).toHaveBeenCalledTimes(1);
    } finally {
      click.mockRestore();
      Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: originalCreateObjectUrl });
      Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: originalRevokeObjectUrl });
    }
  });
});
