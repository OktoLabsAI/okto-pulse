import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ExcalidrawImportDialog } from '../ExcalidrawImportDialog';

describe('ExcalidrawImportDialog', () => {
  it('parses JSON and calls onImport with title and payload', async () => {
    const onImport = vi.fn().mockResolvedValue(undefined);
    render(
      <ExcalidrawImportDialog
        open
        onClose={vi.fn()}
        onImport={onImport}
      />,
    );

    fireEvent.change(screen.getByDisplayValue('Imported architecture'), {
      target: { value: 'Checkout architecture' },
    });
    fireEvent.change(screen.getAllByRole('textbox')[2], {
      target: {
        value: JSON.stringify({
          type: 'excalidraw',
          version: 2,
          elements: [{ id: 'shape_1', type: 'rectangle' }],
          appState: {},
          files: {},
        }),
      },
    });
    fireEvent.click(screen.getByText('Import'));

    await waitFor(() => expect(onImport).toHaveBeenCalledTimes(1));
    expect(onImport.mock.calls[0][0]).toMatchObject({
      title: 'Checkout architecture',
      diagramType: 'container',
      payload: {
        elements: [{ id: 'shape_1', type: 'rectangle' }],
      },
    });
  });

  // Spec cc497a0d — ts_semantic_frontend_import
  it('surfaces normalization warnings from onValidate and proceeds with import', async () => {
    const onImport = vi.fn().mockResolvedValue(undefined);
    const onValidate = vi.fn().mockResolvedValue({
      valid: true,
      warnings: ['diagrams[0].adapter_payload.elements[0] semantic_metadata_normalized: filled [displayType,iconName]'],
      issues: [],
      suggested_fixes: [],
    });
    const onClose = vi.fn();
    render(
      <ExcalidrawImportDialog
        open
        onClose={onClose}
        onImport={onImport}
        onValidate={onValidate}
      />,
    );

    fireEvent.change(screen.getAllByRole('textbox')[2], {
      target: {
        value: JSON.stringify({ type: 'excalidraw', version: 2, elements: [] }),
      },
    });
    fireEvent.click(screen.getByText('Import'));

    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onImport).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('blocks Import button and shows suggested_fixes when validation rejects payload', async () => {
    const onImport = vi.fn();
    const onValidate = vi.fn().mockResolvedValue({
      valid: false,
      warnings: [],
      issues: ['diagrams[0].adapter_payload.elements[0].iconName=\'foobar\' is not in the allowed icon set.'],
      suggested_fixes: ['Use one of the allowed iconName values or remove the field to inherit from the registry.'],
    });
    render(
      <ExcalidrawImportDialog
        open
        onClose={vi.fn()}
        onImport={onImport}
        onValidate={onValidate}
      />,
    );

    fireEvent.change(screen.getAllByRole('textbox')[2], {
      target: { value: JSON.stringify({ type: 'excalidraw', version: 2, elements: [] }) },
    });
    fireEvent.click(screen.getByText('Import'));

    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(1));
    expect(onImport).not.toHaveBeenCalled();
    expect(screen.getByTestId('excalidraw-import-issues')).toHaveTextContent('not in the allowed icon set');
    expect(screen.getByTestId('excalidraw-import-fixes')).toHaveTextContent('inherit from the registry');
    // Import button disabled after a failed validation
    expect(screen.getByText('Fix issues first')).toBeDisabled();
  });
});
