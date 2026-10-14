import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { EditableField } from '../EditableField';

describe('EditableField live updates', () => {
  it('retains the draft and requires explicit review before overwriting a new saved value', async () => {
    const save = vi.fn().mockResolvedValue(undefined);
    const view = render(<EditableField value="Original" onSave={save} />);
    fireEvent.click(screen.getByText('Original'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'My unsaved draft' } });
    view.rerender(<EditableField value="Changed through MCP" onSave={save} />);
    expect(screen.getByRole('textbox')).toHaveValue('My unsaved draft');
    fireEvent.blur(screen.getByRole('textbox'));
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
    expect(save).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('Current value: Changed through MCP');
    fireEvent.click(screen.getByRole('button', { name: 'Keep my draft after reviewing this value' }));
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
    await waitFor(() => expect(save).toHaveBeenCalledWith('My unsaved draft'));
  });

  it('can discard the draft for the current value without writing', () => {
    const save = vi.fn();
    const view = render(<EditableField value="Original" onSave={save} />);
    fireEvent.click(screen.getByText('Original'));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Draft' } });
    view.rerender(<EditableField value="Current" onSave={save} />);
    fireEvent.click(screen.getByRole('button', { name: 'Use current value' }));
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.getByText('Current')).toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
  });
});
