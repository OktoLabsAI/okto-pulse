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
});
