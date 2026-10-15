import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TextRequirementsTab } from '../TextRequirementsTab';
import { AuthenticatedFetchError } from '@/lib/authFetch';

const rows = [{ id: 'fr-a', text: 'Same content', title: 'First title' }, { id: 'fr-b', text: 'Same content', title: 'Second title' }];
function props(extra = {}) { return { kind: 'FR' as const, version: 8, items: rows, canCreate: true, canEdit: true, canRevoke: true, onSave: vi.fn().mockResolvedValue(undefined), onRevoke: vi.fn().mockResolvedValue(undefined), ...extra }; }
describe('structured FR and AC cards', () => {
  it.each(['version_conflict', 'impact_ack_required'])('distinguishes native %s from a generic HTTP conflict', async code => {
    const value = props({ onSave: vi.fn().mockRejectedValue(new AuthenticatedFetchError({
      status: 409, message: 'Native refusal', details: { error_code: code },
    })) });
    const view = render(<TextRequirementsTab {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit fr-a' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Native refusal');
    expect(screen.getByRole('button', { name: 'Save' }).hasAttribute('disabled')).toBe(code === 'version_conflict');
    expect(screen.getByLabelText('Content')).toHaveValue('Same content');
    if (code === 'version_conflict') {
      expect(screen.getByRole('button', { name: 'Review requirement base' })).toBeDisabled();
      view.rerender(<TextRequirementsTab {...value} version={9} />);
      fireEvent.click(screen.getByRole('button', { name: 'Review requirement base' }));
      expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    } else expect(screen.queryByRole('button', { name: 'Review requirement base' })).not.toBeInTheDocument();
  });
  it('preserves a draft and requires explicit base review after an external version change', async () => {
    const value = props(); const view = render(<TextRequirementsTab {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit fr-a' }));
    fireEvent.change(screen.getByLabelText('Content'), { target: { value: 'Local obligation' } });
    view.rerender(<TextRequirementsTab {...value} version={9} items={[{ ...rows[0], text: 'Remote obligation' }, rows[1]]} />);
    expect(screen.getByLabelText('Content')).toHaveValue('Local obligation');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.getByText('Remote obligation')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Review requirement base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(value.onSave).toHaveBeenCalledExactlyOnceWith('fr-a', { title: 'First title', text: 'Local obligation' }, 9));
  });
  it('keeps a removed requirement draft visible without recreating it or permitting save', () => {
    const value = props(); const view = render(<TextRequirementsTab {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit fr-a' }));
    view.rerender(<TextRequirementsTab {...value} version={9} items={[rows[1]]} />);
    expect(screen.getByLabelText('Title')).toHaveValue('First title');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.getByText(/This requirement is no longer active/)).toBeInTheDocument();
    expect(value.onSave).not.toHaveBeenCalled();
  });
  it('blocks a draft on a failed current read without discarding its fields', () => {
    const value = props(); const view = render(<TextRequirementsTab {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit fr-a' }));
    view.rerender(<TextRequirementsTab {...value} readCurrent={false} />);
    expect(screen.getByLabelText('Title')).toHaveValue('First title');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(value.onSave).not.toHaveBeenCalled();
  });
  it('edits title and content by stable ID even when two items share content', async () => {
    const value = props(); render(<TextRequirementsTab {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Edit fr-b' }));
    expect(screen.getByLabelText('Title')).toHaveValue('Second title');
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Updated title' } });
    fireEvent.change(screen.getByLabelText('Content'), { target: { value: 'Updated body' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(value.onSave).toHaveBeenCalledExactlyOnceWith('fr-b', { title: 'Updated title', text: 'Updated body' }, 8));
    expect(value.onRevoke).not.toHaveBeenCalled();
  });
  it('creates an AC with separate title and content', async () => {
    const value = props({ kind: 'AC' as const, items: [] }); render(<TextRequirementsTab {...value} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add AC' }));
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Observable result' } });
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Content'), { target: { value: 'Given an invalid request, no write occurs.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(value.onSave).toHaveBeenCalledWith(null, { title: 'Observable result', text: 'Given an invalid request, no write occurs.' }, 8));
  });
  it('shows AC titles in compact rows and expands the independently stored content', () => {
    render(<TextRequirementsTab {...props({ kind: 'AC' as const })} />);
    expect(screen.getByRole('button', { name: /First title/ })).toHaveTextContent('fr-a');
    expect(screen.queryByText('Same content')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /First title/ }));
    expect(screen.getByText('Same content')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
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
    expect(screen.queryByText('Existing content')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Title not defined/ }));
    expect(screen.getByText('Existing content')).toBeInTheDocument();
    expect(screen.getByText(/Title not defined/)).toBeInTheDocument();
    expect(screen.queryByText('Revoked')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Edit|Revoke|Add/ })).not.toBeInTheDocument();
    expect(value.onSave).not.toHaveBeenCalled();
  });
  it('uses compact expandable rows and task-link coverage like Integration', () => {
    render(<TextRequirementsTab {...props({ items: [{ ...rows[0], linked_task_ids: ['task-one'] }, rows[1], { id: 'revoked', text: 'Old', status: 'revoked' as const }] })} />);
    expect(screen.getByRole('progressbar', { name: 'FR task links' })).toHaveAttribute('aria-valuenow', '50');
    expect(screen.getByText('FR Task Coverage (1/2)')).toBeInTheDocument();
    expect(screen.queryByText('Same content')).not.toBeInTheDocument();
    const first = screen.getByRole('button', { name: /First title/ });
    expect(first).toHaveTextContent('fr-a');
    fireEvent.click(first);
    expect(screen.getByText('Same content')).toHaveClass('text-xs');
    expect(first).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Linked Tasks:')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Second title/ }));
    expect(first).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Linked Tasks:')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add Functional Requirement' })).toBeInTheDocument();
  });
});
