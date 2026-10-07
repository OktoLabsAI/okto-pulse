import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useDashboardApi } from '../api';

const mockApiClient = {
  fetchJson: vi.fn(),
  fetch: vi.fn(),
};

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => mockApiClient,
}));

describe('paginated list API surface', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetchJson.mockResolvedValue({
      items: [],
      total_filtered: 0,
      total_overall: 0,
      offset: 0,
      limit: 25,
    });
  });

  it('requests list envelopes with offset, limit, filters and cancellation', async () => {
    const controller = new AbortController();
    const { result } = renderHook(() => useDashboardApi());

    await result.current.listStoriesPage('board-1', {
      offset: 25,
      limit: 25,
      status: 'ready',
      topicId: 'topic-1',
      search: 'server query',
      linked: true,
      converted: false,
      includeArchived: true,
      signal: controller.signal,
    });
    await result.current.listIdeationsPage('board-1', {
      offset: 50,
      limit: 50,
      status: 'done',
      search: 'idea query',
      derivationPending: true,
      includeArchived: true,
      signal: controller.signal,
    });
    await result.current.listSpecsPage('board-1', {
      offset: 100,
      limit: 100,
      status: 'validated',
      search: 'spec query',
      signal: controller.signal,
    });
    await result.current.listBoardRefinementsPage('board-1', {
      offset: 0,
      limit: 25,
      status: 'done',
      search: 'needle',
      derivationPending: true,
      labels: ['api', 'ux'],
      signal: controller.signal,
    });


    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      1,
      '/boards/board-1/stories?offset=25&limit=25&status=ready&topic_id=topic-1&search=server+query&linked=true&converted=false&include_archived=true',
      { signal: controller.signal },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/boards/board-1/ideations?offset=50&limit=50&status=done&search=idea+query&derivation_pending=true&include_archived=true',
      { signal: controller.signal },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      3,
      '/boards/board-1/specs?offset=100&limit=100&status=validated&search=spec+query',
      { signal: controller.signal },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      4,
      '/boards/board-1/refinements?offset=0&limit=25&status=done&search=needle&derivation_pending=true&labels=api%2Cux',
      { signal: controller.signal },
    );
  });

  it.each(['spec', 'ideation', 'story', 'refinement'] as const)(
    'loads every %s candidate through the native page contract', async (kind) => {
      const first = Array.from({ length: 100 }, (_, index) => ({ id: `item-${index}` }));
      const last = { id: 'item-100' };
      mockApiClient.fetchJson
        .mockResolvedValueOnce({ items: first, offset: 0, limit: 100, total_filtered: 101, total_overall: 150 })
        .mockResolvedValueOnce({ items: [last], offset: 100, limit: 100, total_filtered: 101, total_overall: 150 });
      const { result } = renderHook(() => useDashboardApi());
      const items = kind === 'spec' ? await result.current.listSpecs('board-1', 'approved')
        : kind === 'ideation' ? await result.current.listIdeations('board-1', 'draft')
          : kind === 'story' ? await result.current.listStories('board-1', { linked: false })
            : await result.current.listRefinements('idea-1');
      expect(items).toEqual([...first, last]);
      expect(mockApiClient.fetchJson).toHaveBeenCalledTimes(2);
      const paths = mockApiClient.fetchJson.mock.calls.map(([path]) => new URL(path, 'https://pulse.test'));
      expect(paths.map((path) => path.searchParams.get('offset'))).toEqual(['0', '100']);
      expect(paths.every((path) => path.searchParams.get('limit') === '100')).toBe(true);
      if (kind === 'spec' || kind === 'ideation') {
        expect(paths.map((path) => path.searchParams.get('status')))
          .toEqual(kind === 'spec' ? ['approved', 'approved'] : ['draft', 'draft']);
      }
      if (kind === 'story') expect(paths.every((path) => path.searchParams.get('linked') === 'false')).toBe(true);
    },
  );

  it('rejects an old array response instead of silently truncating selector options', async () => {
    mockApiClient.fetchJson.mockResolvedValue([{ id: 'old' }]);
    const { result } = renderHook(() => useDashboardApi());
    await expect(result.current.listSpecs('board-1')).rejects.toThrow('Invalid list page response');
    expect(mockApiClient.fetchJson).toHaveBeenCalledTimes(1);
  });

  it('reports an incomplete page instead of returning a partial selector list', async () => {
    mockApiClient.fetchJson.mockResolvedValue({ items: [], offset: 0, limit: 100, total_filtered: 1 });
    const { result } = renderHook(() => useDashboardApi());
    await expect(result.current.listIdeations('board-1')).rejects.toThrow('Incomplete list page response');
  });
});
