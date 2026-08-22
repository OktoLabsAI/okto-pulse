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

describe('Spec dependency API surface', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetchJson.mockResolvedValue({
      items: [],
      total: 0,
      limit: 25,
      has_more: false,
      next_cursor: undefined,
      readiness: {
        board_id: 'board/1',
        spec_id: 'spec 1',
        current_edition: 2,
        last_started_edition: null,
        current_edition_started: false,
        active_dependency_count: 0,
        unmet_count: 0,
        blocking_count: 0,
        archived_blocking_count: 0,
        unfinished_blocking_count: 0,
        blockers_truncated: false,
        blockers: [],
        can_start: true,
        ready: true,
        reason_code: null,
      },
    });
  });

  it('encodes the deterministic cursor and every supported filter', async () => {
    const signal = new AbortController().signal;
    const { result } = renderHook(() => useDashboardApi());

    await result.current.listSpecDependencies('board/1', 'spec 1', {
      direction: 'required_by',
      active_state: 'removed',
      satisfaction: 'unmet',
      retrospective: false,
      lineage: 'cross_ideation',
      related_statuses: ['approved'],
      cursor: 'opaque+/=',
      limit: 50,
      signal,
    });

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/boards/board%2F1/specs/spec%201/dependencies?direction=required_by&limit=50&cursor=opaque%2B%2F%3D&satisfaction=unmet&retrospective=false&active_state=removed&lineage=cross_ideation&related_status=approved',
      { signal },
    );
  });

  it('sends fenced, idempotent add and removal commands', async () => {
    const signal = new AbortController().signal;
    const { result } = renderHook(() => useDashboardApi());

    await result.current.addSpecDependency('board-1', 'spec-1', {
      prerequisite_spec_id: 'spec-2',
      expected_spec_version: 7,
      expected_spec_edition: 3,
      idempotency_key: 'add-key',
    }, signal);
    await result.current.removeSpecDependency('board-1', 'spec-1', 'dep/1', {
      expected_spec_version: 8,
      expected_spec_edition: 3,
      idempotency_key: 'remove-key',
      reason: 'The prerequisite was superseded.',
    }, signal);

    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      1,
      '/boards/board-1/specs/spec-1/dependencies',
      {
        method: 'POST',
        body: JSON.stringify({
          prerequisite_spec_id: 'spec-2',
          expected_spec_version: 7,
          expected_spec_edition: 3,
          idempotency_key: 'add-key',
        }),
        signal,
      },
    );
    expect(mockApiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/boards/board-1/specs/spec-1/dependencies/dep%2F1',
      {
        method: 'DELETE',
        body: JSON.stringify({
          expected_spec_version: 8,
          expected_spec_edition: 3,
          idempotency_key: 'remove-key',
          reason: 'The prerequisite was superseded.',
        }),
        signal,
      },
    );
  });
});
