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

describe('permission preset API lineage surface', () => {
  beforeEach(() => {
    mockApiClient.fetchJson.mockReset();
    mockApiClient.fetch.mockReset();
  });

  it('uses the clone endpoint so the backend retains base_preset_id', async () => {
    mockApiClient.fetchJson.mockResolvedValue({
      id: 'clone-1',
      base_preset_id: 'base-1',
    });
    const { result } = renderHook(() => useDashboardApi());
    const payload = {
      name: 'Spec copy',
      description: 'Cloned from Spec',
      flags: { spec: { quality: { read: true } } },
    };

    await result.current.clonePreset('base-1', payload);

    expect(mockApiClient.fetchJson).toHaveBeenCalledWith(
      '/presets/base-1/clone',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
    );
  });
});
