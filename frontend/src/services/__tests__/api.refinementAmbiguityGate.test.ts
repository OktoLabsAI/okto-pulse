import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useDashboardApi } from '../api';

const mockApiClient = vi.hoisted(() => ({
  fetchJson: vi.fn(),
  fetch: vi.fn(),
}));

vi.mock('@/contexts/ApiContext', () => ({
  useApiClient: () => mockApiClient,
}));

describe('refinement ambiguity gate skip API', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetch.mockReset();
  });

  it('sends the human-only write contract with reason and optimistic version', async () => {
    mockApiClient.fetchJson.mockResolvedValue({
      skipped: true,
      activity_id: 'activity-1',
      version: 7,
    });
    const { result } = renderHook(() => useDashboardApi());

    const receipt = await result.current.setRefinementAmbiguityGateSkip(
      'refinement-1',
      {
        skip_ambiguity_gate: true,
        reason: 'Accepted risk for this delivery.',
        expected_refinement_edition: 2,
        expected_refinement_version: 7,
      },
    );

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/refinements/refinement-1/ambiguity-gate-skip',
      {
        method: 'PATCH',
        body: JSON.stringify({
          skip_ambiguity_gate: true,
          reason: 'Accepted risk for this delivery.',
          expected_refinement_edition: 2,
          expected_refinement_version: 7,
        }),
      },
    );
    expect(receipt).toEqual({
      skipped: true,
      activity_id: 'activity-1',
      version: 7,
    });
  });

  it('fails closed before I/O for a blank reason or invalid version', async () => {
    const { result } = renderHook(() => useDashboardApi());

    await expect(result.current.setRefinementAmbiguityGateSkip(
      'refinement-1',
      {
        skip_ambiguity_gate: true,
        reason: '   ',
        expected_refinement_edition: 2,
        expected_refinement_version: 7,
      },
    )).rejects.toThrow('non-empty reason');

    await expect(result.current.setRefinementAmbiguityGateSkip(
      'refinement-1',
      {
        skip_ambiguity_gate: true,
        reason: 'Accepted risk.',
        expected_refinement_edition: 2,
        expected_refinement_version: 0,
      },
    )).rejects.toThrow('valid expected refinement version');

    expect(mockApiClient.fetchJson).not.toHaveBeenCalled();
  });
});
