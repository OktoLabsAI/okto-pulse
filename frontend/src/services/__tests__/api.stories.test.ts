import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { useDashboardApi } from '../api';

const mockApiClient = { fetchJson: vi.fn(), fetch: vi.fn() };
vi.mock('@/contexts/ApiContext', () => ({ useApiClient: () => mockApiClient }));

describe('Story conversion API', () => {
  it('creates an Ideation through the single supported route', async () => {
    const response = { success: true, ideation: { id: 'idea-1' }, links: [] };
    mockApiClient.fetchJson.mockResolvedValueOnce(response);
    const { result } = renderHook(() => useDashboardApi());
    const request = { story_ids: ['story-1'] };

    await expect(result.current.convertStories('board-1', request)).resolves.toEqual(response);

    expect(mockApiClient.fetchJson).toHaveBeenCalledTimes(1);
    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/boards/board-1/stories/convert-to-ideation',
      { method: 'POST', body: JSON.stringify(request) },
    );
  });
});
