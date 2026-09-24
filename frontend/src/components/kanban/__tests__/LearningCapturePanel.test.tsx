import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LearningCapturePanel } from '../LearningCapturePanel';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { CaptureSource } from '@/services/learning-capture-api';

const mocks = vi.hoisted(() => ({ api: { source: vi.fn(), history: vi.fn(), create: vi.fn() },
  denied: new Set<string>(), loading: false, error: null as Error | null }));
vi.mock('@/services/learning-capture-api', () => ({ useLearningCaptureApi: () => mocks.api }));
vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => ({
  has: (flag: string) => !mocks.denied.has(flag), isLoading: mocks.loading, error: mocks.error,
}) }));
const source: CaptureSource = { source_digest: 'a'.repeat(64), source_policy_version: 1,
  scenarios: [{ id: 'proof', title: 'Signed inspection', authenticated: true },
    { id: 'legacy', title: 'Legacy note', authenticated: false }] };
const item = { learning_id: 'learning', generation: 0, source_revision: 0, fingerprint: 'f'.repeat(64),
  capture: { capture_id: 'old', author_id: 'original-author', captured_at: '2026-09-24T15:00:00Z',
    content: '<script>old learning</script>', context: 'Old context', applicability: 'Old scope',
    source: { digest: 'b'.repeat(64), policy_version: 1 } } };
beforeEach(() => {
  mocks.denied.clear(); mocks.loading = false; mocks.error = null;
  mocks.api.source.mockReset().mockResolvedValue(source);
  mocks.api.history.mockReset().mockResolvedValue({ items: [], next_cursor: null });
  mocks.api.create.mockReset().mockResolvedValue({ capture_id: 'saved', learning_id: 'learning' });
});
afterEach(cleanup);
function panel(bugId = 'bug') { return <LearningCapturePanel boardId="board" bugId={bugId} />; }
async function fill() {
  fireEvent.change(screen.getByLabelText('Learning', { exact: true }), { target: { value: 'Authored lesson' } });
  fireEvent.change(screen.getByLabelText('Context', { exact: true }), { target: { value: 'Release' } });
  fireEvent.change(screen.getByLabelText('Applicability', { exact: true }), { target: { value: 'Compiled releases' } });
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Signed inspection' }));
}

describe('LearningCapturePanel', () => {
  it('uses authenticated evidence and preserves one request identity after a network failure', async () => {
    mocks.api.create.mockRejectedValueOnce(new Error('SECRET provider')).mockResolvedValueOnce({});
    render(panel()); await fill();
    expect(screen.getByRole('checkbox', { name: /Legacy note/ })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Save Learning' }));
    expect(await screen.findByRole('alert')).not.toHaveTextContent('SECRET');
    expect(screen.getByLabelText('Learning', { exact: true })).toHaveValue('Authored lesson');
    fireEvent.click(screen.getByRole('button', { name: 'Save Learning' }));
    await screen.findByText('Learning saved. Graph materialization is pending.');
    expect(mocks.api.create.mock.calls[0][1]).toEqual(mocks.api.create.mock.calls[1][1]);
    expect(mocks.api.create.mock.calls[0][1]).toMatchObject({ board_id: 'board',
      content: 'Authored lesson', expected_source_digest: source.source_digest, scenario_ids: ['proof'] });
    expect(mocks.api.create.mock.calls[0][1]).not.toHaveProperty('author_id');
    expect(screen.getByRole('button', { name: 'Save Learning' })).toBeDisabled();
  });

  it('requires refreshing a stale basis without losing text and sends a new intent after review', async () => {
    mocks.api.create.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 409, message: 'SECRET' })).mockResolvedValueOnce({});
    render(panel()); await fill();
    fireEvent.click(screen.getByRole('button', { name: 'Save Learning' }));
    await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: 'Save Learning' })).toBeDisabled();
    mocks.api.source.mockResolvedValue({ ...source, source_digest: 'c'.repeat(64) });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh evidence' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save Learning' })).toBeEnabled());
    expect(screen.getByLabelText('Learning', { exact: true })).toHaveValue('Authored lesson');
    fireEvent.click(screen.getByRole('button', { name: 'Save Learning' }));
    await screen.findByText('Learning saved. Graph materialization is pending.');
    const [first, next] = mocks.api.create.mock.calls.map(call => call[1]);
    expect(next.capture_id).not.toBe(first.capture_id);
    expect(next.expected_source_digest).toBe('c'.repeat(64));
  });

  it('keeps creation available without permission to read cognitive history', async () => {
    mocks.denied.add('kg.query.learning_from_bugs'); render(panel()); await fill();
    expect(screen.getByRole('button', { name: 'Save Learning' })).toBeEnabled();
    expect(mocks.api.history).not.toHaveBeenCalled();
    expect(screen.queryByRole('heading', { name: 'Saved Learnings' })).not.toBeInTheDocument();
  });

  it('renders history as inert authored text with no current approval inference', async () => {
    mocks.denied.add('kg.session.commit'); mocks.api.history.mockResolvedValue({ items: [item], next_cursor: 'next' });
    const { container } = render(panel());
    await screen.findByText(item.capture.content);
    expect(container.querySelector('script')).toBeNull();
    expect(screen.queryByLabelText('Learning', { exact: true })).not.toBeInTheDocument();
    expect(screen.getByText(/earlier evidence basis/)).toBeInTheDocument();
    mocks.api.history.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 403, message: 'PRIVATE' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next Learning page' }));
    expect(screen.queryByText(item.capture.content)).not.toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent('Learning access is unavailable');
    expect(mocks.api.history.mock.calls[1][2]).toBe('next');
  });

  it('does not interpret unavailable history as empty and supports refresh', async () => {
    mocks.api.history.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 503, message: 'SECRET' }));
    render(panel()); await screen.findByRole('alert');
    expect(screen.queryByText('No saved Learnings on this page.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh saved Learnings' }));
    await screen.findByText('No saved Learnings on this page.');
  });

  it.each(['Card', 'Board'])('aborts old source/history and clears the draft on %s change', async changed => {
    let resolve!: (value: CaptureSource) => void;
    mocks.api.source.mockReturnValueOnce(new Promise<CaptureSource>(done => { resolve = done; }));
    const { rerender } = render(panel());
    fireEvent.change(screen.getByLabelText('Learning', { exact: true }), { target: { value: 'Private draft' } });
    const oldSignal = mocks.api.source.mock.calls[0][2] as AbortSignal;
    const oldHistorySignal = mocks.api.history.mock.calls[0][3] as AbortSignal;
    rerender(changed === 'Card' ? panel('other-bug') : <LearningCapturePanel boardId="other-board" bugId="bug" />);
    expect(oldSignal.aborted).toBe(true);
    expect(oldHistorySignal.aborted).toBe(true);
    expect(screen.getByLabelText('Learning', { exact: true })).toHaveValue('');
    await screen.findByRole('checkbox', { name: 'Signed inspection' });
    await act(async () => resolve({ ...source, scenarios: [{ id: 'secret', title: 'Old private scenario', authenticated: true }] }));
    expect(screen.queryByText('Old private scenario')).not.toBeInTheDocument();
  });

  it.each(['loading', 'error', 'denied'])('makes no requests when source authority is %s', state => {
    if (state === 'loading') mocks.loading = true;
    else if (state === 'error') mocks.error = new Error('private');
    else mocks.denied.add('spec.tests.read');
    render(panel());
    expect(mocks.api.source).not.toHaveBeenCalled(); expect(mocks.api.history).not.toHaveBeenCalled();
  });
});
