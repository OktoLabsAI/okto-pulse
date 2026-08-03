import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ImportExportButtons } from './ImportExportButtons';
import type {
  ImportExportEnvelope,
  ImportSummary,
} from '@/services/import-export-api';

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

const summary = (overrides: Partial<ImportSummary> = {}): ImportSummary => ({
  created: 0,
  skipped: [],
  errors: [],
  ...overrides,
});

const envelope = (id: string, kind = 'design_systems'): ImportExportEnvelope => ({
  schema_version: '1',
  kind,
  items: [{ id, title: id }],
});

describe('ImportExportButtons', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('merges one or many selected envelopes into one bulk import', async () => {
    const onImport = vi.fn().mockResolvedValue(summary({ created: 2 }));
    render(
      <ImportExportButtons
        kind="design_systems"
        onExport={vi.fn()}
        onImport={onImport}
      />,
    );

    const files = [
      new File([JSON.stringify(envelope('d1'))], 'd1.json', { type: 'application/json' }),
      new File([JSON.stringify(envelope('d2'))], 'd2.json', { type: 'application/json' }),
    ];
    fireEvent.change(screen.getByTestId('design_systems-import-input'), {
      target: { files },
    });

    await waitFor(() => expect(onImport).toHaveBeenCalledTimes(1));
    expect(onImport.mock.calls[0][0].items).toEqual([
      { id: 'd1', title: 'd1' },
      { id: 'd2', title: 'd2' },
    ]);
  });

  it('requires confirmation before replacing a non-versioned same-id item', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const onImport = vi
      .fn()
      .mockResolvedValueOnce(summary({
        skipped: [{ id: 'p1', reason: 'replacement_requires_confirmation' }],
        dry_run: true,
      }))
      .mockResolvedValueOnce(summary({ replaced: 1 }));
    render(
      <ImportExportButtons
        kind="presets"
        onExport={vi.fn()}
        onImport={onImport}
        confirmReplacements
      />,
    );

    fireEvent.change(screen.getByTestId('presets-import-input'), {
      target: {
        files: [new File(
          [JSON.stringify(envelope('p1', 'presets'))],
          'p1.json',
          { type: 'application/json' },
        )],
      },
    });

    await waitFor(() => expect(onImport).toHaveBeenCalledTimes(2));
    expect(onImport.mock.calls[0][1]).toEqual({ dryRun: true });
    expect(onImport.mock.calls[1][1]).toEqual({ replaceExisting: true });
    expect(window.confirm).toHaveBeenCalledTimes(1);
  });

  it('does not replace when the user declines confirmation', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const onImport = vi.fn().mockResolvedValue(summary({
      skipped: [{ id: 'p1', reason: 'replacement_requires_confirmation' }],
      dry_run: true,
    }));
    const onImported = vi.fn();
    render(
      <ImportExportButtons
        kind="presets"
        onExport={vi.fn()}
        onImport={onImport}
        onImported={onImported}
        confirmReplacements
      />,
    );

    fireEvent.change(screen.getByTestId('presets-import-input'), {
      target: {
        files: [new File(
          [JSON.stringify(envelope('p1', 'presets'))],
          'p1.json',
          { type: 'application/json' },
        )],
      },
    });

    await waitFor(() => expect(window.confirm).toHaveBeenCalledTimes(1));
    expect(onImport).toHaveBeenCalledTimes(1);
    expect(onImported).not.toHaveBeenCalled();
  });
});
