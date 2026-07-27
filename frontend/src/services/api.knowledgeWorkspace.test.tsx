import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchJson = vi.hoisted(() => vi.fn());

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => ({ fetchJson }),
}));

import { useDashboardApi } from './api';

describe('Knowledge Workspace API projections', () => {
  beforeEach(() => {
    fetchJson.mockReset();
  });

  it('uses explicit legacy mode for existing hydrated-map callers', async () => {
    fetchJson.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'spec',
      entity_id: 'spec-1',
      resources: { architecture: [], mockup: [], knowledge_base: [] },
    });
    const { result } = renderHook(() => useDashboardApi());

    const response = await result.current.getEffectiveResources(
      'board-1',
      'spec',
      'spec-1',
    );

    const url = new URL(fetchJson.mock.calls[0][0], 'http://local');
    expect(url.pathname).toBe('/resource-gate/spec/spec-1/effective-resources');
    expect(url.searchParams.get('board_id')).toBe('board-1');
    expect(url.searchParams.get('profile')).toBe('legacy');
    expect(response.profile).toBe('legacy');
  });

  it('forwards bounded profile, opaque cursor and limit and normalizes resources', async () => {
    fetchJson.mockResolvedValue({
      contract_version: 2,
      board_id: 'board-1',
      entity_type: 'card',
      entity_id: 'card-1',
      profile: 'summary',
      items: [],
      count: 0,
      total_count: 0,
      next_cursor: null,
      truncated: false,
      unique_effective_count: 0,
      raw_attachment_count: 0,
      workspace_item_count: 0,
      unique_root_version_count: 0,
      response_bytes: 300,
    });
    const { result } = renderHook(() => useDashboardApi());

    const response = await result.current.getEffectiveResources(
      'board-1',
      'card',
      'card-1',
      { profile: 'summary', cursor: 'opaque-next', limit: 25 },
    );

    const url = new URL(fetchJson.mock.calls[0][0], 'http://local');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      board_id: 'board-1',
      profile: 'summary',
      cursor: 'opaque-next',
      limit: '25',
    });
    expect(response.resources).toEqual({
      architecture: [],
      mockup: [],
      knowledge_base: [],
    });
  });

  it('detects a legacy response when an older server ignores a bounded profile', async () => {
    const legacyResources = {
      architecture: [],
      mockup: [],
      knowledge_base: [
        {
          id: 'legacy-kb',
          title: 'Hydrated by the old server',
          resource: { content: 'legacy body' },
        },
      ],
    };
    fetchJson.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'card',
      entity_id: 'card-1',
      resources: legacyResources,
    });
    const { result } = renderHook(() => useDashboardApi());

    const response = await result.current.getEffectiveResources(
      'board-1',
      'card',
      'card-1',
      { profile: 'summary', limit: 25 },
    );

    expect(response.profile).toBe('legacy');
    expect(response.resources).toBe(legacyResources);
    expect(response.items).toBeUndefined();
  });
});
