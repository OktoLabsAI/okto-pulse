import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { SpecCoverageResponse } from './specCoverageTypes';
const api = vi.hoisted(() => ({ getSpecCoverage: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
import { SpecCoveragePanel } from './SpecCoveragePanel';

const base: SpecCoverageResponse = {
  view: 'coverage', subject_ref: 'spec:spec', edition: 1, authority: 'informational', data_source: 'composed',
  projection_freshness: { state: 'incomplete', graph_generation: 'one', source_checkpoint: 'source',
    projection_checkpoint: null, checked_at: '2026-10-01T00:00:00Z' },
  completeness: { complete_for_scope: false, truncated: false, limitations: [] },
  structure: { authority: 'spec_coverage_summary', interpretation: 'planning_links_not_delivery_proof',
    complete_for_scope: true, summary: { ac_covered: 1, ac_total: 1, fr_covered: 0, fr_total: 1,
      scenarios_linked: 1, scenarios_total: 1 }, graph: { state: 'observed', complete_for_scope: false,
      expected_nodes: 3, observed_nodes: 2, missing_nodes: 1, missing_relations: 1 } },
  delivery: { state: 'available', authority: 'evaluate_delivery_coverage', complete_for_scope: true,
    counts: { obligations: 2, implementation_proven: 0, verification_proven: 0, observed_obligations: 2 },
    blockers: [], rejected_record_refs: [], interpretation: 'informational' }, items: [], next_cursor: null,
};
function page(title: string, next: string | null = null): SpecCoverageResponse {
  return { ...base, next_cursor: next, items: [{ kind: 'delivery', title, obligation_ref: title,
    semantic_sha256: 'digest', implementation: 'missing', verification: 'missing',
    implementation_record_refs: [], verification_record_refs: [], implementation_waiver_refs: [],
    verification_waiver_refs: [], required_card_refs: [], missing_card_refs: [], missing_criteria: [] }] };
}
beforeEach(() => { vi.clearAllMocks(); api.getSpecCoverage.mockResolvedValue(base); });

it('keeps linked Test Cards separate from passing proof and graph absence', async () => {
  api.getSpecCoverage.mockResolvedValue(page('Unverified requirement'));
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findByText('Unverified requirement')).toBeInTheDocument();
  expect(screen.getByText(/Scenarios → Test Cards: 1\/1/)).toBeInTheDocument();
  expect(screen.getByText(/Verification: Proof missing/)).toBeInTheDocument();
  expect(screen.getByText(/Verification proven: 0/)).toBeInTheDocument();
  expect(screen.getByText(/Graph freshness: incomplete/)).toBeInTheDocument();
  expect(screen.getByText(/does not approve a gate/)).toBeInTheDocument();
});

it('paginates a pinned observation without adding whole-scope counts', async () => {
  api.getSpecCoverage.mockResolvedValueOnce(page('first', 'cursor')).mockResolvedValueOnce(page('second'));
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Load more coverage' }));
  expect(await screen.findByText('second')).toBeInTheDocument();
  expect(screen.getByText('first')).toBeInTheDocument();
  expect(api.getSpecCoverage.mock.calls[1][2]).toEqual({ limit: 200, cursor: 'cursor' });
  expect(screen.getByText(/Obligations: 2/)).toBeInTheDocument();
});

it.each([403, 409, 503])('clears earlier data on HTTP %s', async status => {
  api.getSpecCoverage.mockResolvedValueOnce(page('first', 'cursor')).mockRejectedValueOnce(new AuthenticatedFetchError({ status, message: 'failure' }));
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Load more coverage' }));
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.queryByText('first')).not.toBeInTheDocument();
});

it('aborts on Spec changes and ignores a late response', async () => {
  let resolveOld!: (value: SpecCoverageResponse) => void;
  api.getSpecCoverage.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; })).mockResolvedValueOnce(page('new-spec'));
  const view = render(<SpecCoveragePanel boardId="board" specId="old" revision="1" onOpenSection={vi.fn()} />);
  const signal = api.getSpecCoverage.mock.calls[0][3] as AbortSignal;
  view.rerender(<SpecCoveragePanel boardId="board" specId="new" revision="1" onOpenSection={vi.fn()} />);
  expect(signal.aborted).toBe(true);
  expect(await screen.findByText('new-spec')).toBeInTheDocument();
  await act(async () => resolveOld(page('old-spec')));
  expect(screen.queryByText('old-spec')).not.toBeInTheDocument();
  view.unmount();
  expect((api.getSpecCoverage.mock.calls[1][3] as AbortSignal).aborted).toBe(true);
});

it('keeps restricted proof unknown rather than zero', async () => {
  api.getSpecCoverage.mockResolvedValue({ ...base, delivery: { ...base.delivery, state: 'restricted',
    counts: { obligations: null, implementation_proven: null, verification_proven: null, observed_obligations: null } } });
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findByText(/Proof access: restricted/)).toHaveTextContent('Verification proven: Unknown');
});

it('correction only opens authorized domain editing and never writes', async () => {
  const navigate = vi.fn();
  const view = render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={navigate} />);
  await screen.findByText(/Functional requirements → rules: 0\/1/);
  expect(screen.queryByRole('button', { name: 'Correct rules links' })).not.toBeInTheDocument();
  view.rerender(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={navigate} canCorrect={['rules']} />);
  fireEvent.click(screen.getByRole('button', { name: 'Correct rules links' }));
  expect(navigate).toHaveBeenCalledWith('rules');
  expect(api.getSpecCoverage).toHaveBeenCalledTimes(1);
});

it('reloads after a source revision without retaining previous pages', async () => {
  const view = render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  await screen.findByText(/Edition 1/);
  view.rerender(<SpecCoveragePanel boardId="board" specId="spec" revision="2" onOpenSection={vi.fn()} />);
  await waitFor(() => expect(api.getSpecCoverage).toHaveBeenCalledTimes(2));
  expect(api.getSpecCoverage.mock.calls[1][2].cursor).toBeUndefined();
});
