import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { workspacePage } from '@/testFixtures/effectiveResources';
import type { ScreenMockup } from '@/types';
import { MockupsTab } from '../MockupsTab';

const api = vi.hoisted(() => ({ getEffectiveResources: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const mockup: ScreenMockup = { id: 'm1', title: 'Existing mockup', description: null, screen_type: 'page',
  html_content: '<main>Existing</main>', annotations: null, order: 0 };
const scope = { boardId: 'board', entityType: 'spec' as const, entityId: 'spec' };

function fill() {
  fireEvent.click(screen.getByRole('button', { name: 'Add Mockup' }));
  fireEvent.change(screen.getByPlaceholderText('e.g. Login Page'), { target: { value: 'Local draft' } });
  fireEvent.change(document.querySelector('textarea')!, { target: { value: '<main>Local content</main>' } });
}

describe('MockupsTab authored collection and recovery', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getEffectiveResources.mockImplementation((board, type, id, options) => Promise.resolve(workspacePage(board, type, id, 'mockup', [], options.profile)));
  });

  it('preserves the draft and uses the reviewed current collection without resurrecting removed items', async () => {
    const onUpdate = vi.fn().mockResolvedValue({ version: 4 });
    const view = render(<MockupsTab screenMockups={[]} version={2} snapshot="old" onUpdate={onUpdate} />);
    fill();
    view.rerender(<MockupsTab screenMockups={[mockup]} version={2} snapshot="current" onUpdate={onUpdate} />);
    expect(screen.getByPlaceholderText('e.g. Login Page')).toHaveValue('Local draft');
    expect(screen.getByRole('button', { name: 'Add Mockup' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Review mockup base' }));
    expect(screen.getByText('Existing mockup · m1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Use reviewed mockup base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add Mockup' }));
    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(onUpdate.mock.calls[0][0]).toEqual([mockup, expect.objectContaining({ title: 'Local draft' })]);
    expect(onUpdate.mock.calls[0][1]).toEqual({ expectedVersion: 2, expectedSnapshot: 'current' });
  });

  it('does not reintroduce an externally removed mockup after reviewing a draft', async () => {
    const onUpdate = vi.fn().mockResolvedValue({ version: 5 });
    const view = render(<MockupsTab screenMockups={[mockup]} version={3} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    fireEvent.change(screen.getByPlaceholderText('e.g. Login Page'), { target: { value: 'New only' } });
    fireEvent.change(document.querySelector('textarea')!, { target: { value: '<main>New</main>' } });
    view.rerender(<MockupsTab screenMockups={[]} version={4} onUpdate={onUpdate} />);
    fireEvent.click(screen.getByRole('button', { name: 'Review mockup base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Use reviewed mockup base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add Mockup' }));
    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(onUpdate.mock.calls[0][0]).toEqual([expect.objectContaining({ title: 'New only' })]);
  });

  it('recovers confirmed creation by reading only, with the returned minimum version', async () => {
    const onUpdate = vi.fn().mockResolvedValue({ version: 3 });
    const onReload = vi.fn().mockRejectedValueOnce(new Error('Read unavailable')).mockResolvedValue(undefined);
    render(<MockupsTab screenMockups={[]} version={2} onUpdate={onUpdate} onReload={onReload} />);
    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Add Mockup' }));
    await screen.findByText('Read unavailable');
    expect(screen.getByRole('button', { name: 'Add Mockup' })).toBeDisabled();
    expect(screen.getByPlaceholderText('e.g. Login Page')).toHaveValue('Local draft');
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved mockups' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Reload saved mockups' })).not.toBeInTheDocument());
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onReload.mock.calls).toEqual([[3], [3]]);
  });

  it('blocks an uncertain response until current resources are inspected and the attempt discarded', async () => {
    const onUpdate = vi.fn().mockRejectedValue(new TypeError('Response lost'));
    const onReload = vi.fn().mockResolvedValue(undefined);
    render(<MockupsTab screenMockups={[]} version={2} onUpdate={onUpdate} onReload={onReload} />);
    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Add Mockup' }));
    await screen.findByText('Response lost');
    expect(screen.getByRole('button', { name: 'Add Mockup' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'I reviewed mockups; discard local attempt' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Reload current mockups' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'I reviewed mockups; discard local attempt' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'I reviewed mockups; discard local attempt' }));
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(screen.queryByPlaceholderText('e.g. Login Page')).not.toBeInTheDocument();
  });

  it('recovers confirmed deletion without a second write and catches failed reads', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const onUpdate = vi.fn().mockResolvedValue({ version: 3 });
    const onReload = vi.fn().mockRejectedValueOnce(new Error('List unavailable')).mockResolvedValue(undefined);
    try {
      render(<MockupsTab screenMockups={[mockup]} version={2} onUpdate={onUpdate} onReload={onReload} />);
      fireEvent.click(screen.getByTitle('Delete mockup'));
      await screen.findByText('List unavailable');
      expect(screen.getByTitle('Delete mockup')).toBeDisabled();
      fireEvent.click(screen.getByRole('button', { name: 'Reload saved mockups' }));
      await waitFor(() => expect(onReload).toHaveBeenCalledTimes(2));
      expect(onUpdate.mock.calls).toEqual([[[], { expectedVersion: 2 }]]);
    } finally { confirm.mockRestore(); }
  });

  it('ignores a late creation result after the parent changes', async () => {
    let finish!: (value: { version: number }) => void;
    const onUpdate = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const onReload = vi.fn();
    const view = render(<MockupsTab screenMockups={[]} version={2} entityId="first" onUpdate={onUpdate} onReload={onReload} />);
    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Add Mockup' }));
    view.rerender(<MockupsTab screenMockups={[mockup]} version={9} entityId="second" onUpdate={onUpdate} onReload={onReload} />);
    await act(async () => finish({ version: 3 }));
    expect(onReload).not.toHaveBeenCalled();
    expect(screen.getByText('Existing mockup')).toBeInTheDocument();
    expect(screen.queryByText(/changes were saved/)).not.toBeInTheDocument();
  });

  it('defers inherited reads while hidden, preserves the draft and rereads on return', async () => {
    const view = render(<MockupsTab {...scope} screenMockups={[]} version={2} onUpdate={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add Mockup' })).toBeEnabled());
    fill();
    const reads = api.getEffectiveResources.mock.calls.length;
    view.rerender(<MockupsTab {...scope} screenMockups={[]} version={3} revision={1} active={false} onUpdate={vi.fn()} />);
    expect(api.getEffectiveResources).toHaveBeenCalledTimes(reads);
    expect(screen.getByPlaceholderText('e.g. Login Page')).toHaveValue('Local draft');
    view.rerender(<MockupsTab {...scope} screenMockups={[]} version={3} revision={1} onUpdate={vi.fn()} />);
    await waitFor(() => expect(api.getEffectiveResources.mock.calls.length).toBeGreaterThan(reads));
    expect(screen.getByRole('button', { name: 'Add Mockup' })).toBeDisabled();
  });

  it('removes protected drafts and content after a denied effective resource read', async () => {
    const view = render(<MockupsTab {...scope} screenMockups={[]} version={2} onUpdate={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add Mockup' })).toBeEnabled());
    fill();
    api.getEffectiveResources.mockRejectedValue(Object.assign(new Error('Denied'), { status: 403 }));
    view.rerender(<MockupsTab {...scope} screenMockups={[]} version={2} revision={1} onUpdate={vi.fn()} />);
    await screen.findByText('Mockup access is no longer available.');
    expect(screen.queryByDisplayValue('Local draft')).not.toBeInTheDocument();
  });
});
