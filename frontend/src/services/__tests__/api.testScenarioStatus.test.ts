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

  it('admits a report with escaped scope and no automatic receipt-producing retry', async () => {
    const evidence = { evidence_class: 'verification_report', execution_receipt: 'opaque' };
    mockApiClient.fetchJson.mockResolvedValue({ evidence });
    const { result } = renderHook(() => useDashboardApi());
    const report = { method: 'inspection' };
    expect(await result.current.admitTestVerificationReport('spec/a', 'ts/b', report)).toEqual({ evidence });
    expect(mockApiClient.fetchJson).toHaveBeenCalledExactlyOnceWith('/specs/spec%2Fa/scenarios/ts%2Fb/evidence/reports', {
      method: 'POST', body: JSON.stringify({ report }), maxRetries: 0,
    });
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

