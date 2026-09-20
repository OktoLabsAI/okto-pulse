import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HistoricalArchivesPanel } from '../HistoricalArchivesPanel';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { ArchiveList, ArchivePage } from '@/services/historical-archives-api';

const api = vi.hoisted(() => ({ list: vi.fn(), read: vi.fn() }));
vi.mock('@/services/historical-archives-api', () => ({ useHistoricalArchivesApi: () => api }));
const item = { origin: { kind: 'sprint', id: 'origin' }, archive_id: 'archive', sections: ['content', 'history'] };
const listing = { items: [item], next_offset: null };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  api.list.mockReset().mockResolvedValue(listing);
  api.read.mockReset().mockResolvedValue({ records: [{ title: 'Original title', related_spec_id: 'old-spec' }], next_offset: null });
});
afterEach(cleanup);

describe('HistoricalArchivesPanel', () => {
  it('loads content on demand, exposes only allowed sections and never executes historical references', async () => {
    render(<HistoricalArchivesPanel boardId="board" />);
    fireEvent.click(await screen.findByRole('button', { name: 'sprint · origin' }));
    expect(await screen.findByText('Original title')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Questions & answers' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Evaluations' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(api.read).toHaveBeenCalledTimes(1);
    expect(api.read.mock.calls[0].slice(0, 4)).toEqual(['board', item, 'content', 0]);
    expect(screen.getByText(/does not approve current work/)).toBeInTheDocument();
  });

  it('clears records when the next section page is denied and does not show backend details', async () => {
    api.read.mockResolvedValueOnce({ records: [{ question: 'PAGE-ONE-SECRET' }], next_offset: 1 })
      .mockRejectedValueOnce(new AuthenticatedFetchError({ status: 404, message: 'PRIVATE-PATH' }));
    render(<HistoricalArchivesPanel boardId="board" />);
    fireEvent.click(await screen.findByRole('button', { name: 'sprint · origin' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Next section page' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('no longer have access');
    expect(screen.queryByText('PAGE-ONE-SECRET')).not.toBeInTheDocument();
    expect(screen.queryByText('PRIVATE-PATH')).not.toBeInTheDocument();
    expect(api.read.mock.calls[1][3]).toBe(1);
  });

  it('aborts old section reads and ignores their late completion', async () => {
    const late = deferred<ArchivePage>();
    api.read.mockReturnValueOnce(late.promise).mockResolvedValueOnce({ records: [{ summary: 'CURRENT-HISTORY' }], next_offset: null });
    render(<HistoricalArchivesPanel boardId="board" />);
    fireEvent.click(await screen.findByRole('button', { name: 'sprint · origin' }));
    await waitFor(() => expect(api.read).toHaveBeenCalledTimes(1));
    const signal = api.read.mock.calls[0][4] as AbortSignal;
    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    expect(await screen.findByText('CURRENT-HISTORY')).toBeInTheDocument();
    expect(signal.aborted).toBe(true);
    await act(async () => late.resolve({ records: [{ title: 'STALE-CONTENT' }], next_offset: null }));
    expect(screen.queryByText('STALE-CONTENT')).not.toBeInTheDocument();
  });

  it('does not retain selected content across Boards or accept late discovery from the previous Board', async () => {
    const late = deferred<ArchiveList>();
    api.list.mockReturnValueOnce(late.promise).mockResolvedValueOnce({ items: [], next_offset: null });
    const { rerender } = render(<HistoricalArchivesPanel boardId="first" />);
    const signal = api.list.mock.calls[0][2] as AbortSignal;
    rerender(<HistoricalArchivesPanel boardId="second" />);
    expect(await screen.findByText('No archived origins available.')).toBeInTheDocument();
    expect(signal.aborted).toBe(true);
    await act(async () => late.resolve(listing as ArchiveList));
    expect(screen.queryByRole('button', { name: 'sprint · origin' })).not.toBeInTheDocument();
  });

  it('removes already loaded records immediately on Board change', async () => {
    const { rerender } = render(<HistoricalArchivesPanel boardId="first" />);
    fireEvent.click(await screen.findByRole('button', { name: 'sprint · origin' }));
    await screen.findByText('Original title');
    api.list.mockResolvedValueOnce({ items: [], next_offset: null });
    rerender(<HistoricalArchivesPanel boardId="second" />);
    expect(screen.queryByText('Original title')).not.toBeInTheDocument();
    await screen.findByText('No archived origins available.');
  });

  it('paginates origins with server offsets and refreshes authority when returning from detail', async () => {
    api.list.mockResolvedValueOnce({ ...listing, next_offset: 1 }).mockResolvedValueOnce(listing)
      .mockResolvedValueOnce({ items: [], next_offset: null });
    render(<HistoricalArchivesPanel boardId="board" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Next origins page' }));
    await waitFor(() => expect(api.list.mock.calls[1][1]).toBe(1));
    fireEvent.click(await screen.findByRole('button', { name: 'sprint · origin' }));
    await screen.findByText('Original title');
    fireEvent.click(screen.getByRole('button', { name: 'Back to archived origins' }));
    expect(await screen.findByText('No archived origins available.')).toBeInTheDocument();
  });

  it.each([413, 503])('distinguishes failure %s from empty results and can retry', async status => {
    api.list.mockRejectedValueOnce(new AuthenticatedFetchError({ status, message: 'PRIVATE' })).mockResolvedValueOnce(listing);
    render(<HistoricalArchivesPanel boardId="board" />);
    expect(await screen.findByRole('alert')).toHaveTextContent(status === 413 ? 'reading limit' : 'could not be verified');
    expect(screen.queryByText('No archived origins available.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry archives' }));
    expect(await screen.findByRole('button', { name: 'sprint · origin' })).toBeInTheDocument();
  });

  it('renders archived HTML as inert original text', async () => {
    const text = '<script>alert(1)</script><a href="javascript:alert(1)">old</a>';
    api.read.mockResolvedValueOnce({ records: [{ description: text }], next_offset: null });
    const { container } = render(<HistoricalArchivesPanel boardId="board" />);
    fireEvent.click(await screen.findByRole('button', { name: 'sprint · origin' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(container.querySelector('script, a')).toBeNull();
  });
});
