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

describe('legacy guideline compatibility during SK-B migration', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
  });

  it('preserves listGuidelines as the legacy offset array contract', async () => {
    const legacy: Guideline = {
      id: 'guideline-1',
      title: 'Legacy guideline',
      content: 'Context for every agent.',
      tags: [],
      scope: 'global',
      board_id: null,
      owner_id: 'owner-1',
      version: 3,
      created_at: '2026-07-30T00:00:00Z',
      updated_at: '2026-07-30T00:00:00Z',
    };
    mockApiClient.fetchJson.mockResolvedValue([legacy]);
    const { result } = renderHook(() => useDashboardApi());

    const guidelines: Guideline[] = await result.current.listGuidelines(
      25,
      50,
      'architecture',
    );

    expect(guidelines).toEqual([legacy]);
    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/guidelines?offset=25&limit=50&tag=architecture',
    );
  });

  it('keeps the legacy mutable model separate from the policy root', () => {
    const legacyScope: GuidelineScope = 'inline';
    const policyRoot: PolicyGuidelineRoot = {
      guideline_id: 'guideline-1',
      owner_id: 'owner-1',
      scope: 'inline',
      board_id: 'board-1',
      context_scope: 'all',
      created_at: '2026-07-30T00:00:00Z',
    };

    expect(legacyScope).toBe(policyRoot.scope);
    expect(policyRoot).not.toHaveProperty('content');
    expect(policyRoot).not.toHaveProperty('version');
  });
});
