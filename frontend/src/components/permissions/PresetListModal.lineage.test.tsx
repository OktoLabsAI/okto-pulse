import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PermissionPreset } from '@/types';
import { PresetListModal } from './PresetListModal';

const apiMock = vi.hoisted(() => ({
  clonePreset: vi.fn(),
  createPreset: vi.fn(),
  deletePreset: vi.fn(),
  listPresets: vi.fn(),
  updatePreset: vi.fn(),
}));
const importExportMock = vi.hoisted(() => ({
  exportPresets: vi.fn(),
  importPresets: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('@/services/import-export-api', () => ({
  useImportExportApi: () => importExportMock,
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
  options: Partial<PermissionPreset> = {},
): PermissionPreset {
  return {
    id,
    owner_id: null,
    name,
    description: null,
    is_builtin: true,
    base_preset_id: null,
    flags: {
      board: { entity: { read: true } },
    },
    owner_review_required: false,
    review_reason: null,
    created_at: '2026-07-27T00:00:00Z',
    updated_at: null,
    ...options,
  };
}

describe('PresetListModal lineage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.createPreset.mockResolvedValue({});
  });

  it('shows each custom base and malformed lineage state explicitly', async () => {
    const executor = preset('executor', 'Executor');
    const fullControl = preset('full', 'Full Control');
    const resolved = preset(
      'resolved',
      'Resolved Custom',
      {
        is_builtin: false,
        base_preset_id: executor.id,
      },
    );
    const dangling = preset(
      'dangling',
      'Dangling Custom',
      {
        is_builtin: false,
        base_preset_id: 'deleted-base',
        owner_review_required: true,
        review_reason: 'dangling_base_preset',
      },
    );
    const cycleA = preset(
      'cycle-a',
      'Cycle A',
      {
        is_builtin: false,
        base_preset_id: 'cycle-b',
        owner_review_required: true,
        review_reason: 'preset_lineage_cycle',
      },
    );
    const cycleB = preset(
      'cycle-b',
      'Cycle B',
      {
        is_builtin: false,
        base_preset_id: 'cycle-a',
        owner_review_required: true,
        review_reason: 'preset_lineage_cycle',
      },
    );
    apiMock.listPresets.mockResolvedValue([
      executor,
      dangling,
      resolved,
      cycleB,
      fullControl,
      cycleA,
    ]);

    render(<PresetListModal onClose={() => {}} />);

    expect(
      await screen.findByTestId('preset-lineage-resolved'),
    ).toHaveTextContent('Base: Executor');
    expect(screen.getByTestId('preset-lineage-resolved')).toHaveTextContent(
      'lineage resolved',
    );
    expect(screen.getByTestId('preset-lineage-dangling')).toHaveTextContent(
      'Base: deleted-base',
    );
    expect(screen.getByTestId('preset-lineage-dangling')).toHaveTextContent(
      /dangling base.*owner review required/,
    );
    expect(screen.getByTestId('preset-lineage-cycle-a')).toHaveTextContent(
      'Base: Cycle B',
    );
    expect(screen.getByTestId('preset-lineage-cycle-a')).toHaveTextContent(
      /lineage cycle.*owner review required/,
    );
  });

  it('creates from the real Full Control shape when Executor is first', async () => {
    const executor = preset(
      'executor',
      'Executor',
      {
        flags: {
          board: { entity: { read: false } },
        },
      },
    );
    const fullControl = preset(
      'full',
      'Full Control',
      {
        flags: {
          board: { entity: { read: true, update: true } },
        },
      },
    );
    apiMock.listPresets.mockResolvedValue([executor, fullControl]);

    render(<PresetListModal onClose={() => {}} />);

    await screen.findByText('Executor');
    fireEvent.click(
      screen.getByRole('button', { name: /^New Preset$/i }),
    );
    fireEvent.change(screen.getByPlaceholderText('Preset name...'), {
      target: { value: 'New Custom' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Edit Board permissions' }),
    );
    expect(
      screen.getByRole('button', { name: 'Toggle board.entity.read' }),
    ).toHaveAttribute('aria-pressed', 'false');
    expect(
      screen.getByRole('button', { name: 'Toggle board.entity.update' }),
    ).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(screen.getByRole('button', { name: 'Create Preset' }));

    await waitFor(() => {
      expect(apiMock.createPreset).toHaveBeenCalledWith({
        name: 'New Custom',
        description: undefined,
        flags: {
          board: { entity: { read: false, update: false } },
        },
      });
    });
  });
});
