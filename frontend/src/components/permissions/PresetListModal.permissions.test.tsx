import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { PresetListModal } from './PresetListModal';
import type { PermissionPreset } from '@/types';

const apiMock = vi.hoisted(() => ({
  clonePreset: vi.fn(),
  createPreset: vi.fn(),
  deletePreset: vi.fn(),
  listPresets: vi.fn(),
  updatePreset: vi.fn(),
}));
const importExportMock = vi.hoisted(() => ({
  exportPreset: vi.fn(),
  exportPresets: vi.fn(),
  importPresets: vi.fn(),
}));
const permissionState = vi.hoisted(() => ({
  allowed: new Set<string>(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'Custom',
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: (flag: string) => permissionState.allowed.has(flag),
  }),
}));
vi.mock('@/services/import-export-api', () => ({
  useImportExportApi: () => importExportMock,
  importExportFilename: (kind: string) => `${kind}-00000000.json`,
  downloadJsonFile: vi.fn(),
}));
vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

const customPreset: PermissionPreset = {
  id: 'custom',
  owner_id: 'owner',
  name: 'Custom',
  description: null,
  is_builtin: false,
  base_preset_id: null,
  flags: { board: { entity: { read: true } } },
  owner_review_required: false,
  review_reason: null,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: null,
};

function grant(...permissions: string[]) {
  permissionState.allowed = new Set(permissions);
}

describe('PresetListModal ADMIN-CATALOG permissions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    grant();
    apiMock.listPresets.mockResolvedValue([customPreset]);
    apiMock.clonePreset.mockResolvedValue({});
    importExportMock.exportPresets.mockResolvedValue({
      schema_version: '1',
      kind: 'presets',
      items: [],
    });
  });

  it('does not read the catalog without permission and keeps import/export independent', async () => {
    grant('permission_preset.export');

    render(<PresetListModal boardId="b1" onClose={() => {}} />);

    expect(await screen.findByTestId('preset-read-unavailable'))
      .toHaveTextContent('permission_preset.entity.read');
    expect(apiMock.listPresets).not.toHaveBeenCalled();
    expect(screen.getByTestId('presets-export')).toBeEnabled();
    expect(screen.getByTestId('presets-import')).toBeDisabled();
    fireEvent.click(screen.getByTestId('presets-export'));
    await waitFor(() => expect(importExportMock.exportPresets).toHaveBeenCalledTimes(1));
  });

  it('enables clone alone while create, edit, delete, import, and export stay denied', async () => {
    grant(
      'permission_preset.entity.read',
      'permission_preset.clone',
    );

    render(<PresetListModal boardId="b1" onClose={() => {}} />);
    await screen.findByText('Custom');

    expect(screen.getByRole('button', { name: /^New Preset$/i })).toBeDisabled();
    expect(screen.getByTitle('Edit')).toBeDisabled();
    expect(screen.getByTitle('Delete')).toBeDisabled();
    expect(screen.getByLabelText('Export Custom')).toBeDisabled();
    expect(screen.getByTestId('presets-import')).toBeDisabled();
    expect(screen.getByTitle('Clone')).toBeEnabled();

    fireEvent.click(screen.getByTitle('Edit'));
    fireEvent.click(screen.getByTitle('Delete'));
    fireEvent.click(screen.getByLabelText('Export Custom'));
    fireEvent.click(screen.getByTitle('Clone'));
    await waitFor(() => expect(apiMock.clonePreset).toHaveBeenCalledWith(
      'custom',
      expect.objectContaining({ name: 'Custom (copy)' }),
    ));
    expect(apiMock.updatePreset).not.toHaveBeenCalled();
    expect(apiMock.deletePreset).not.toHaveBeenCalled();
    expect(importExportMock.exportPreset).not.toHaveBeenCalled();
  });
});
