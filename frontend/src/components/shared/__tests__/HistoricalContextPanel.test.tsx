import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HistoricalContextPanel } from '../HistoricalContextPanel';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { HistoricalContextPage } from '@/services/historical-context-api';

const api = vi.hoisted(() => ({ read: vi.fn() }));
vi.mock('@/services/historical-context-api', () => ({ useHistoricalContextApi: () => api }));
const item = { binding_id: 'binding', origin: { kind: 'sprint', id: 'original' }, archive_id: 'archive',
  section: 'qa' as const, field: null, record: { question: 'Original question', asked_by: 'original-author', answer: null } };
const page: HistoricalContextPage = { items: [item], next_offset: null };
function deferred() {
  let resolve!: (value: HistoricalContextPage) => void;
  const promise = new Promise<HistoricalContextPage>(done => { resolve = done; });
  return { promise, resolve };
}
beforeEach(() => api.read.mockReset().mockResolvedValue(page));
afterEach(cleanup);

describe('HistoricalContextPanel', () => {
  it('shows original attribution as inert read-only content with no inferred approval', async () => {
    const text = '<script>alert(1)</script><a href="javascript:alert(1)">old</a>';
    api.read.mockResolvedValue({ ...page, items: [{ ...item, record: { ...item.record, question: text } }] });
    const { container } = render(<HistoricalContextPanel boardId="board" targetKind="card" targetId="card" />);
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByText('original-author')).toBeInTheDocument();
    expect(screen.getByText(/sprint · original/)).toBeInTheDocument();
    expect(screen.getByText(/does not approve current work/)).toBeInTheDocument();
    expect(container.querySelector('script, a, input, textarea, select')).toBeNull();
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Refresh context']);
    expect(api.read.mock.calls[0].slice(0, 4)).toEqual(['board', 'card', 'card', 0]);
  });

  it('clears previous records before a denied next page and uses the server offset', async () => {
    api.read.mockResolvedValueOnce({ ...page, next_offset: 1 }).mockRejectedValueOnce(new AuthenticatedFetchError({ status: 404, message: 'PRIVATE-PATH' }));
    render(<HistoricalContextPanel boardId="board" targetKind="spec" targetId="spec" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Next context page' }));
    expect(screen.queryByText('Original question')).not.toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent('no longer have access');
    expect(screen.queryByText('PRIVATE-PATH')).not.toBeInTheDocument();
    expect(api.read.mock.calls[1][3]).toBe(1);
  });

  it.each(['board', 'targetId', 'targetKind'] as const)('aborts and hides stale records immediately on %s change', async changed => {
    const late = deferred();
    api.read.mockReturnValueOnce(late.promise).mockResolvedValueOnce({ items: [], next_offset: null });
    const initial = { boardId: 'board', targetKind: 'card' as const, targetId: 'card' };
    const { rerender } = render(<HistoricalContextPanel {...initial} />);
    const signal = api.read.mock.calls[0][4] as AbortSignal;
    const props = changed === 'board' ? { ...initial, boardId: 'other' }
      : changed === 'targetId' ? { ...initial, targetId: 'other' } : { ...initial, targetKind: 'spec' as const };
    rerender(<HistoricalContextPanel {...props} />);
    expect(signal.aborted).toBe(true);
    await screen.findByText('No historical context available.');
    await act(async () => late.resolve(page));
    expect(screen.queryByText('Original question')).not.toBeInTheDocument();
  });

  it('removes already loaded content on destination change and while refreshing', async () => {
    const { rerender } = render(<HistoricalContextPanel boardId="board" targetKind="card" targetId="first" />);
    await screen.findByText('Original question');
    const late = deferred();
    api.read.mockReturnValueOnce(late.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh context' }));
    expect(screen.queryByText('Original question')).not.toBeInTheDocument();
    await act(async () => late.resolve(page));
    await screen.findByText('Original question');
    api.read.mockResolvedValueOnce({ items: [], next_offset: null });
    rerender(<HistoricalContextPanel boardId="board" targetKind="card" targetId="second" />);
    expect(screen.queryByText('Original question')).not.toBeInTheDocument();
    await screen.findByText('No historical context available.');
  });

  it.each([413, 503])('distinguishes %s from an empty result and retries', async status => {
    api.read.mockRejectedValueOnce(new AuthenticatedFetchError({ status, message: 'SECRET' })).mockResolvedValueOnce(page);
    render(<HistoricalContextPanel boardId="board" targetKind="spec" targetId="spec" />);
    expect(await screen.findByRole('alert')).toHaveTextContent(status === 413 ? 'reading limit' : 'could not be verified');
    expect(screen.queryByText('No historical context available.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry context' }));
    expect(await screen.findByText('Original question')).toBeInTheDocument();
    await waitFor(() => expect(api.read).toHaveBeenCalledTimes(2));
  });
});
