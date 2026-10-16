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

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'Full Control',
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: () => true,
  }),
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

  it.each([false, true])('saves text without resubmitting policy (owner review: %s)', async (ownerReview) => {
    const custom = preset('custom', 'Custom', { board: { read: false } }, {
      is_builtin: false, owner_review_required: ownerReview,
      review_reason: ownerReview ? 'invalid_preset_flags' : null,
    });
    render(<PresetEditorModal preset={custom} presets={[custom]} onClose={() => {}} onSaved={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText('Preset name...'), { target: { value: 'Renamed' } });
    fireEvent.change(screen.getByPlaceholderText('What this preset is for...'), { target: { value: 'Description only' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Preset' }));
    await waitFor(() => expect(apiMock.updatePreset).toHaveBeenCalledWith('custom', {
      name: 'Renamed', description: 'Description only',
    }));
    expect(apiMock.updatePreset.mock.calls[0][1]).not.toHaveProperty('flags');
  });

  it('submits an explicit flag edit while review remains visible until the owner saves', async () => {
    const custom = preset('custom', 'Custom', { board: { read: false } }, {
      is_builtin: false, owner_review_required: true, review_reason: 'invalid_preset_flags',
    });
    render(<PresetEditorModal preset={custom} presets={[custom]} onClose={() => {}} onSaved={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Enable All' }));
    expect(screen.getByTestId('preset-lineage-custom')).toHaveTextContent('owner review required');
    expect(apiMock.updatePreset).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save Preset' }));
    await waitFor(() => expect(apiMock.updatePreset).toHaveBeenCalledWith('custom', {
      name: 'Custom', description: undefined, flags: { board: { read: true } },
    }));
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
      id: 'migration-review',
      name: 'Migrated custom',
      base_preset_id: 'valid-base',
      review_reason: 'invalid_preset_flags',
      expected: 'owner review',
      canReset: true,
      catalog: [preset('valid-base', 'Valid base', {})],
    },
    {
      id: 'damaged-review',
      name: 'Damaged provenance',
      base_preset_id: 'valid-base',
      review_reason: 'invalid_permission_migration_review',
      expected: 'owner review',
      canReset: true,
      catalog: [preset('valid-base', 'Valid base', {})],
    },
    {
      id: 'dangling',
      name: 'Dangling',
      base_preset_id: 'missing',
      review_reason: 'dangling_base_preset',
      expected: 'dangling base',
      canReset: false,
      catalog: [] as PermissionPreset[],
    },
    {
      id: 'cycle-a',
      name: 'Cycle A',
      base_preset_id: 'cycle-b',
      review_reason: 'preset_lineage_cycle',
      expected: 'lineage cycle',
      canReset: false,
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
    'shows $expected without silently clearing review',
    ({
      id,
      name,
      base_preset_id,
      review_reason,
      expected,
      canReset,
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
      if (canReset) {
        fireEvent.click(screen.getByRole('button', { name: 'Reset to Base' }));
        expect(lineage).toHaveTextContent('owner review required');
        expect(apiMock.updatePreset).not.toHaveBeenCalled();
      } else {
        expect(screen.queryByRole('button', { name: 'Reset to Base' })).not.toBeInTheDocument();
      }
    },
  );
});
