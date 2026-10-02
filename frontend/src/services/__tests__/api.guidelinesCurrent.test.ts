import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Guideline, GuidelineScope } from '@/types';
import type { PolicyGuidelineRoot } from '@/types/policy-governance';
import { useDashboardApi } from '../api';

const mockApiClient = {
  fetchJson: vi.fn(),
  fetch: vi.fn(),
};

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => mockApiClient,
}));

describe('current guideline context and governed revisions', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
  });

  it('lists native guideline context with bounded pagination', async () => {
    const context: Guideline = {
      id: 'guideline-1',
      title: 'Native guideline',
      content: 'Context for every agent.',
      tags: [],
      scope: 'global',
      board_id: null,
      owner_id: 'owner-1',
      version: 3,
      created_at: '2026-07-30T00:00:00Z',
      updated_at: '2026-07-30T00:00:00Z',
    };
    mockApiClient.fetchJson.mockResolvedValue([context]);
    const { result } = renderHook(() => useDashboardApi());

    const guidelines: Guideline[] = await result.current.listGuidelines(
      25,
      50,
      'architecture',
    );

    expect(guidelines).toEqual([context]);
    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/guidelines?offset=25&limit=50&tag=architecture',
    );
  });

  it('exposes only current mutation clients', () => {
    const { result } = renderHook(() => useDashboardApi());
    for (const name of ['updateGuideline', 'deleteGuideline', 'linkGuidelineToBoard', 'updateGuidelinePriority']) {
      expect(result.current).not.toHaveProperty(name);
    }
  });

  it('keeps the read projection separate from the immutable identity', () => {
    const contextScope: GuidelineScope = 'inline';
    const policyRoot: PolicyGuidelineRoot = {
      guideline_id: 'guideline-1',
      owner_id: 'owner-1',
      scope: 'inline',
      board_id: 'board-1',
      context_scope: 'all',
      created_at: '2026-07-30T00:00:00Z',
    };

    expect(contextScope).toBe(policyRoot.scope);
    expect(policyRoot).not.toHaveProperty('content');
    expect(policyRoot).not.toHaveProperty('version');
  });
});
