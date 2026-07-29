import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PermissionPreset } from '@/types';
import { PresetEditorModal } from './PresetEditorModal';

const apiMock = vi.hoisted(() => ({
  clonePreset: vi.fn(),
  createPreset: vi.fn(),
  updatePreset: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('react-hot-toast', () => ({
  default: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function preset(
  id: string,
  name: string,
  flags: Record<string, unknown>,
  options: Partial<PermissionPreset> = {},
): PermissionPreset {
  return {
    id,
    owner_id: null,
    name,
    description: null,
    is_builtin: true,
    base_preset_id: null,
    flags: flags as PermissionPreset['flags'],
    owner_review_required: false,
    review_reason: null,
    created_at: '2026-07-27T00:00:00Z',
    updated_at: null,
    ...options,
  };
}

describe('PresetEditorModal lineage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.updatePreset.mockResolvedValue({});
  });

  it('resets to base_preset_id, not the first built-in, when order is shuffled', async () => {
    const fullControl = preset(
      'full',
      'Full Control',
      { board: { entity: { read: true, update: true } } },
    );
    const executor = preset(
      'executor',
      'Executor',
      { board: { entity: { read: false, update: true } } },
    );
    const custom = preset(
      'custom',
      'Custom Spec',
      { board: { entity: { read: true, update: false } } },
      {
        is_builtin: false,
        base_preset_id: executor.id,
      },
    );
    const catalog = [fullControl, custom, executor];

    render(
      <PresetEditorModal
        preset={custom}
        presets={catalog}
        onClose={() => {}}
        onSaved={() => {}}
      />,
    );

    expect(screen.getByTestId('preset-lineage-custom')).toHaveTextContent(
      'Base: Executor',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Reset to Base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Preset' }));

    await waitFor(() => {
      expect(apiMock.updatePreset).toHaveBeenCalledWith('custom', {
        name: 'Custom Spec',
        description: undefined,
        flags: executor.flags,
      });
    });
  });

  it.each([
    {
      id: 'dangling',
      name: 'Dangling',
      base_preset_id: 'missing',
      review_reason: 'dangling_base_preset',
      expected: 'dangling base',
      catalog: [] as PermissionPreset[],
    },
    {
      id: 'cycle-a',
      name: 'Cycle A',
      base_preset_id: 'cycle-b',
      review_reason: 'preset_lineage_cycle',
      expected: 'lineage cycle',
      catalog: [
        preset(
          'cycle-b',
          'Cycle B',
          {},
          {
            is_builtin: false,
            base_preset_id: 'cycle-a',
            owner_review_required: true,
            review_reason: 'preset_lineage_cycle',
          },
        ),
      ],
    },
  ])(
    'shows $expected and disables unsafe reset',
    ({
      id,
      name,
      base_preset_id,
      review_reason,
      expected,
      catalog,
    }) => {
      const custom = preset(
        id,
        name,
        {},
        {
          is_builtin: false,
          base_preset_id,
          owner_review_required: true,
          review_reason,
        },
      );

      render(
        <PresetEditorModal
          preset={custom}
          presets={[...catalog, custom]}
          onClose={() => {}}
          onSaved={() => {}}
        />,
      );

      const lineage = screen.getByTestId(`preset-lineage-${id}`);
      expect(lineage).toHaveTextContent(expected);
      expect(lineage).toHaveTextContent('owner review required');
      expect(
        screen.queryByRole('button', { name: 'Reset to Base' }),
      ).not.toBeInTheDocument();
    },
  );
});
