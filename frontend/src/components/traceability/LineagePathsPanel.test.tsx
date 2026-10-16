import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { SourceLineageResponse } from './lineageQueryTypes';
const api = vi.hoisted(() => ({ getSourceLineage: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
import { LineagePathsPanel } from './LineagePathsPanel';

const base: SourceLineageResponse = {
  view: 'lineage', subject_ref: 'spec:0', authority: 'informational', data_source: 'relational',
  scope: { max_depth: 3, path_selection: 'one_shortest_path_per_target',
    interpretation: 'workflow_origins_dependencies_and_amendments_not_execution_or_delivery' },
  projection_freshness: { state: 'unknown', graph_generation: null, source_checkpoint: 'source',
    projection_checkpoint: null, checked_at: '2026-10-01T00:00:00Z' },
  completeness: { complete_for_scope: false, truncated: true, limitations: ['exploration_horizon_reached'] },
  counts: { source_nodes: 6, source_relations: 5, reached_targets: 3 },
  frontier_refs: ['spec:4'], items: [], next_cursor: null,
};
function page(title: string, next: string | null = null): SourceLineageResponse {
  return { ...base, next_cursor: next, items: [{ subject_ref: title, title, entity_type: 'amendment_hotfix_revision',
    status: 'draft', depth: 1, path: [{ source_ref: 'amendment_hotfix_revision:a', relation: 'amendment_of',
      target_ref: 'spec:0', direction: 'incoming', provenance_ref: 'amendment_hotfix_revision:a:original_spec_id' }] }] };
}
const props = { boardId: 'board', subjectRef: 'spec:0', revision: 1, onContinue: vi.fn() };
beforeEach(() => { vi.clearAllMocks(); api.getSourceLineage.mockResolvedValue(base); });

it('separates partial amendment association from whole-Spec supersedence', async () => {
  api.getSourceLineage.mockResolvedValue(page('Partial correction'));
  render(<LineagePathsPanel {...props} />);
  expect(await screen.findByText(/Partial correction · amendment_hotfix_revision · draft/)).toBeInTheDocument();
  expect(screen.getByText(/does not supersede the whole original Spec/)).toBeInTheDocument();
  expect(screen.getByText(/amendment_of.*incoming/)).toBeInTheDocument();
  expect(screen.getByText(/Graph projection: not read; freshness remains unknown/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /execute|start|repair/i })).not.toBeInTheDocument();
});

it('expands beyond three hops and offers explicit continuation from the frontier', async () => {
  render(<LineagePathsPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Explore more hops' }));
  await waitFor(() => expect(api.getSourceLineage).toHaveBeenCalledTimes(2));
  expect(api.getSourceLineage.mock.calls[1][1]).toEqual({ subject_ref: 'spec:0', limit: 200, max_depth: 6, cursor: undefined });
  expect(screen.getByText(/frontier is not exhausted/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Continue from spec:4' }));
  expect(props.onContinue).toHaveBeenCalledWith('spec:4');
});

it('keeps source completeness distinct from graph freshness', async () => {
  api.getSourceLineage.mockResolvedValue({ ...base, completeness: { complete_for_scope: true, truncated: false, limitations: [] }, frontier_refs: [] });
  render(<LineagePathsPanel {...props} />);
  expect(await screen.findByText(/Source scope: complete/)).toHaveTextContent('Graph projection: not read; freshness remains unknown');
  expect(screen.queryByRole('button', { name: 'Explore more hops' })).not.toBeInTheDocument();
});

it('paginates without adding whole-scope counts', async () => {
  api.getSourceLineage.mockResolvedValueOnce(page('first', 'cursor')).mockResolvedValueOnce(page('second'));
  render(<LineagePathsPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Load more source paths' }));
  expect(await screen.findByText(/second · amendment_hotfix_revision/)).toBeInTheDocument();
  expect(screen.getByText(/first · amendment_hotfix_revision/)).toBeInTheDocument();
  expect(screen.getByText(/Source nodes: 6/)).toBeInTheDocument();
  expect(api.getSourceLineage.mock.calls[1][1].cursor).toBe('cursor');
});

it.each([403, 409, 503, 504])('discards the previous observation on HTTP %s', async status => {
  api.getSourceLineage.mockResolvedValueOnce(page('first', 'cursor')).mockRejectedValueOnce(new AuthenticatedFetchError({ status, message: 'failure' }));
  render(<LineagePathsPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Load more source paths' }));
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.queryByText(/first · amendment_hotfix_revision/)).not.toBeInTheDocument();
});

it('aborts scope changes and ignores abandoned responses', async () => {
  let resolveOld!: (value: SourceLineageResponse) => void;
  api.getSourceLineage.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; })).mockResolvedValueOnce(page('new'));
  const view = render(<LineagePathsPanel {...props} />);
  const signal = api.getSourceLineage.mock.calls[0][2] as AbortSignal;
  view.rerender(<LineagePathsPanel {...props} boardId="other" subjectRef="spec:new" />);
  expect(signal.aborted).toBe(true);
  expect(await screen.findByText(/new · amendment_hotfix_revision/)).toBeInTheDocument();
  await act(async () => resolveOld(page('old')));
  expect(screen.queryByText(/old · amendment_hotfix_revision/)).not.toBeInTheDocument();
  view.unmount();
  expect((api.getSourceLineage.mock.calls[1][2] as AbortSignal).aborted).toBe(true);
});

it('reloads revisions and keeps unavailable endpoints explicit', async () => {
  api.getSourceLineage.mockResolvedValue({ ...base, completeness: { ...base.completeness, limitations: ['source_endpoint_unavailable'] } });
  const view = render(<LineagePathsPanel {...props} />);
  expect(await screen.findByText(/source endpoint is unavailable/)).toBeInTheDocument();
  view.rerender(<LineagePathsPanel {...props} revision={2} />);
  await waitFor(() => expect(api.getSourceLineage).toHaveBeenCalledTimes(2));
  expect(api.getSourceLineage.mock.calls[1][1].cursor).toBeUndefined();
});
