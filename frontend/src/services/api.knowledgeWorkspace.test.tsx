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

  it('defaults to current summary without synthesizing resources', async () => {
    fetchJson.mockResolvedValue({
      board_id: 'board-1',
      entity_type: 'spec',
      entity_id: 'spec-1',
      contract_version: 2, profile: 'summary', resource_type: 'knowledge_base', items: [],
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
    expect(url.searchParams.get('profile')).toBe('summary');
    expect(response.profile).toBe('summary');
  });

  it('forwards bounded profile, opaque cursor and limit without normalizing old resources', async () => {
    fetchJson.mockResolvedValue({
      contract_version: 2,
      resource_type: 'knowledge_base',
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
      resource_type: 'knowledge_base',
      cursor: 'opaque-next',
      limit: '25',
    });
    expect(response.items).toEqual([]);
    expect(response).not.toHaveProperty('resources');
  });

  it('refuses an old hydrated response without translating it', async () => {
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

    await expect(result.current.getEffectiveResources(
      'board-1', 'card', 'card-1', { profile: 'summary', limit: 25 },
    )).rejects.toThrow('Invalid effective resources response contract.');
  });
});
