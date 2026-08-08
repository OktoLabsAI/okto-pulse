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

describe('test scenario status API surface', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetch.mockReset();
  });

  it('uses the scoped PATCH without replacing the parent scenario list', async () => {
    mockApiClient.fetchJson.mockResolvedValue({
      id: 'spec-1',
      scenario: {
        id: 'scenario-1',
        status: 'ready',
      },
      result: {
        scenario_id: 'scenario-1',
        old_status: 'draft',
        new_status: 'ready',
        evidence_provided: false,
        evidence_gate_skipped: false,
      },
    });
    const { result } = renderHook(() => useDashboardApi());

    await result.current.updateTestScenarioStatus(
      'spec-1',
      'scenario-1',
      { status: 'ready' },
    );

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/specs/spec-1/scenarios/scenario-1/status',
      {
        method: 'PATCH',
        body: JSON.stringify({ status: 'ready' }),
      },
    );
  });
});

