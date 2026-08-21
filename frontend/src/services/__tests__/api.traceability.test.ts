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

describe('Traceability graph API surface', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetchJson.mockResolvedValue({
      board_id: 'board/1',
      selected: { entity_type: 'spec', entity_id: 'spec 1' },
      root_ideation: { id: 'spec 1', title: 'Spec 1' },
      resolution_path: [],
      nodes: [],
      edges: [],
      summary: {},
      warnings: [],
    });
  });

  it('preserves the legacy lineage URL when no view is supplied', async () => {
    const { result } = renderHook(() => useDashboardApi());

    await result.current.getLineageGraph('board/1', 'spec', 'spec 1', false);

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/boards/board/1/lineage-graph?entity_type=spec&entity_id=spec+1&include_artifacts=false',
    );
  });

  it('keeps the selected-entity dependency scope as the backward-compatible default', async () => {
    const { result } = renderHook(() => useDashboardApi());

    await result.current.getLineageGraph(
      'board-1',
      'task',
      'task-1',
      false,
      'dependency',
    );

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/boards/board-1/lineage-graph?entity_type=task&entity_id=task-1&include_artifacts=false&view=dependency',
    );
  });

  it('requests the full-lineage dependency overlay explicitly and lazily', async () => {
    const { result } = renderHook(() => useDashboardApi());

    await result.current.getLineageGraph(
      'board-1',
      'task',
      'task-1',
      false,
      'dependency',
      'lineage',
    );

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/boards/board-1/lineage-graph?entity_type=task&entity_id=task-1&include_artifacts=false&view=dependency&dependency_scope=lineage',
    );
  });
});
