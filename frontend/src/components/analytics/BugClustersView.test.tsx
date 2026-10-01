import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { BugClustersResponse } from './bugClustersTypes';
const api = vi.hoisted(() => ({ getBugClusters: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
import { BugClustersView } from './BugClustersView';

const empty: BugClustersResponse = {
  view: 'bugs', subject_ref: 'board:one', group_by: 'proxy',
  window: { from: '2026-09-01T00:00:00Z', to: '2026-09-16T00:00:00Z' },
  authority: 'informational', data_source: 'mixed_relational_graph',
  projection_freshness: { state: 'unknown', graph_generation: 'generation', source_checkpoint: null,
    projection_checkpoint: null, checked_at: '2026-10-01T00:00:00Z' },
  completeness: { complete_for_scope: false, truncated: false, limitations: ['graph_unknown'] },
  distinct_bug_count: 2, observed_bug_count: 2, cluster_count: null, observed_cluster_count: 0,
  resolution_semantics: 'latest_verified_done_transition_for_current_done_bugs; missing_timestamp_is_unknown',
  items: [], next_cursor: null,
};
function page(title: string, next: string | null = null): BugClustersResponse {
  return { ...empty, next_cursor: next, observed_cluster_count: 2, items: [{ target_ref: title, title,
    validity: 'unknown', assertion_basis: 'origin_proxy', causal_conclusion: 'not_established',
    distinct_bug_count: null, observed_bug_count: 1, observed_done_count: 1,
    observed_resolution_timestamp_count: 0, observed_median_resolution_hours: null,
    bug_refs: ['card:bug'], provenance_refs: ['writer-rule'], projection_freshness: 'unknown' }] };
}
beforeEach(() => { vi.clearAllMocks(); api.getBugClusters.mockResolvedValue(empty); });

it('defaults to 15 UTC calendar days and distinguishes unknown observations from zero', async () => {
  render(<BugClustersView boardId="one" onBack={vi.fn()} />);
  expect(await screen.findByText('No clusters found in the available observations.')).toBeInTheDocument();
  const query = api.getBugClusters.mock.calls[0][1];
  expect((Date.parse(query.to) - Date.parse(query.from)) / 86400000).toBe(14);
  expect(query.group_by).toBe('proxy');
  expect(screen.getByText(/Clusters in scope: Unknown/)).toBeInTheDocument();
  expect(screen.getByText(/do not establish a common cause/)).toBeInTheDocument();
});

it('uses the server window for later pages and never adds global counts', async () => {
  api.getBugClusters.mockResolvedValueOnce(page('first', 'cursor')).mockResolvedValueOnce(page('second'));
  render(<BugClustersView boardId="one" onBack={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Load more clusters' }));
  expect(await screen.findByText('second')).toBeInTheDocument();
  expect(screen.getByText('first')).toBeInTheDocument();
  expect(api.getBugClusters.mock.calls[1][1]).toMatchObject({ ...empty.window, cursor: 'cursor' });
  expect(screen.getByText(/Distinct Bugs in scope: 2/)).toBeInTheDocument();
  expect(screen.queryByText(/Distinct Bugs in scope: 4/)).not.toBeInTheDocument();
});

it.each([403, 409, 503])('clears earlier pages after HTTP %s and allows a fresh reload', async status => {
  api.getBugClusters.mockResolvedValueOnce(page('first', 'cursor')).mockRejectedValueOnce(
    new AuthenticatedFetchError({ message: 'failure', status }));
  render(<BugClustersView boardId="one" onBack={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Load more clusters' }));
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.queryByText('first')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
  await waitFor(() => expect(api.getBugClusters).toHaveBeenCalledTimes(3));
  expect(api.getBugClusters.mock.calls[2][1].cursor).toBeUndefined();
});

it('aborts a previous Board request and ignores late completion', async () => {
  let resolveOld!: (value: BugClustersResponse) => void;
  api.getBugClusters.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; }))
    .mockResolvedValueOnce(page('new-board'));
  const view = render(<BugClustersView key="one" boardId="one" onBack={vi.fn()} />);
  const signal = api.getBugClusters.mock.calls[0][2] as AbortSignal;
  view.rerender(<BugClustersView key="two" boardId="two" onBack={vi.fn()} />);
  expect(signal.aborted).toBe(true);
  expect(await screen.findByText('new-board')).toBeInTheDocument();
  await act(async () => resolveOld(page('old-board')));
  expect(screen.queryByText('old-board')).not.toBeInTheDocument();
});

it('applies own status and severity, drops the previous cursor, and aborts on unmount', async () => {
  const view = render(<BugClustersView boardId="one" onBack={vi.fn()} />);
  await screen.findByText('No clusters found in the available observations.');
  fireEvent.change(screen.getByLabelText('Group by'), { target: { value: 'severity' } });
  fireEvent.change(screen.getByLabelText('Severity'), { target: { value: 'major' } });
  fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'done' } });
  fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
  await waitFor(() => expect(api.getBugClusters).toHaveBeenCalledTimes(2));
  expect(api.getBugClusters.mock.calls[1][1]).toMatchObject({ group_by: 'severity', severity: 'major', status: 'done' });
  view.unmount();
  expect((api.getBugClusters.mock.calls[1][2] as AbortSignal).aborted).toBe(true);
});

it('uses a conclusive empty state only for complete scopes', async () => {
  api.getBugClusters.mockResolvedValue({ ...empty, group_by: 'severity', cluster_count: 0, distinct_bug_count: 0,
    completeness: { complete_for_scope: true, truncated: false, limitations: [] } });
  render(<BugClustersView boardId="one" onBack={vi.fn()} />);
  expect(await screen.findByText('No clusters in this scope.')).toBeInTheDocument();
});
