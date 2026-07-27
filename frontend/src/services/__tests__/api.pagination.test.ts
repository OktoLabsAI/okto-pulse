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

  it('opts all five list surfaces into envelopes with offset, limit, filters and cancellation', async () => {
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
    await result.current.listBoardSprintsPage('board-1', {
      offset: 25,
      limit: 25,
      status: 'in_progress',
      specId: 'spec-1',
      search: 'sprint query',
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
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      5,
      '/boards/board-1/sprints?offset=25&limit=25&status=in_progress&spec_id=spec-1&search=sprint+query',
      { signal: controller.signal },
    );
  });
});
