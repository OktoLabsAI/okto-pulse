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

describe('Project structure REST client', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetchJson.mockResolvedValue({});
  });

  it('uses the dedicated encoded Spec and Card projection routes', async () => {
    const signal = new AbortController().signal;
    const { result } = renderHook(() => useDashboardApi());

    await result.current.getProjectStructure('board/1', 'spec 1', signal);
    await result.current.getCardProjectStructure('board/1', 'card 1', signal);

    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      1,
      '/boards/board%2F1/specs/spec%201/project-structure',
      { signal },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/boards/board%2F1/cards/card%201/project-structure',
      { signal },
    );
  });

  it('passes the canonical CAS/idempotency batch without transport aliases', async () => {
    const signal = new AbortController().signal;
    const { result } = renderHook(() => useDashboardApi());
    const request = {
      expected_spec_version: 7,
      expected_structure_revision: 11,
      idempotency_key: 'project-structure-ui-key',
      operations: [{
        operation: 'link_test' as const,
        entity_id: 'psn_entry',
        test_id: 'test-1',
        test_role: 'target' as const,
      }],
    };

    await result.current.mutateProjectStructure('board/1', 'spec 1', request, signal);

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/boards/board%2F1/specs/spec%201/project-structure',
      {
        method: 'PATCH',
        body: JSON.stringify(request),
        signal,
      },
    );
    expect(JSON.parse(mockApiClient.fetchJson.mock.calls[0][1].body)).not.toHaveProperty('node_id');
  });
});
