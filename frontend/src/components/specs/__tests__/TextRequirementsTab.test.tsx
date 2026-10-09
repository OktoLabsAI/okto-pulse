import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TextRequirementsTab } from '../TextRequirementsTab';

const rows = [{ id: 'fr-a', text: 'Same content', title: 'First title' }, { id: 'fr-b', text: 'Same content', title: 'Second title' }];
function props(extra = {}) { return { kind: 'FR' as const, items: rows, canCreate: true, canEdit: true, canRevoke: true, onSave: vi.fn().mockResolvedValue(undefined), onRevoke: vi.fn().mockResolvedValue(undefined), ...extra }; }
describe('structured FR and AC cards', () => {
  it('edits title and content by stable ID even when two items share content', async () => {
    const value = props(); render(<TextRequirementsTab {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit fr-b' }));
    expect(screen.getByLabelText('Title')).toHaveValue('Second title');
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Updated title' } });
    fireEvent.change(screen.getByLabelText('Content'), { target: { value: 'Updated body' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(value.onSave).toHaveBeenCalledExactlyOnceWith('fr-b', { title: 'Updated title', text: 'Updated body' }));
    expect(value.onRevoke).not.toHaveBeenCalled();
  });
  it('creates an AC with separate title and content', async () => {
    const value = props({ kind: 'AC' as const, items: [] }); render(<TextRequirementsTab {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add AC' }));
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Observable result' } });
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Content'), { target: { value: 'Given an invalid request, no write occurs.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(value.onSave).toHaveBeenCalledWith(null, { title: 'Observable result', text: 'Given an invalid request, no write occurs.' }));
  });
  it('keeps a refused draft and suppresses concurrent writes', async () => {
    let reject!: (error: Error) => void;
    const value = props({ onSave: vi.fn(() => new Promise<void>((_, fail) => { reject = fail; })) });
    render(<TextRequirementsTab {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit fr-a' }));
    const save = screen.getByRole('button', { name: 'Save' }); fireEvent.click(save); fireEvent.click(save);
    expect(value.onSave).toHaveBeenCalledOnce(); reject(new Error('Spec version conflict'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Spec version conflict');
    expect(screen.getByLabelText('Content')).toHaveValue('Same content');
  });
  it('revokes only the selected ID and leaves the other duplicate untouched', async () => {
    const value = props(); render(<TextRequirementsTab {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Revoke fr-b' }));
    await waitFor(() => expect(value.onRevoke).toHaveBeenCalledExactlyOnceWith('fr-b'));
    expect(value.onSave).not.toHaveBeenCalled();
  });
  it('renders existing content without inventing a title or offering unauthorized actions', () => {
    const value = props({ items: [{ id: 'fr-a', text: 'Existing content' }, { id: 'fr-old', text: 'Revoked', status: 'revoked' as const }], canCreate: false, canEdit: false, canRevoke: false });
    render(<TextRequirementsTab {...value} />);
    expect(screen.getByText('Existing content')).toBeInTheDocument();
    expect(screen.getByText('Title not defined')).toBeInTheDocument();
    expect(screen.queryByText('Revoked')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(value.onSave).not.toHaveBeenCalled();
  });
});
