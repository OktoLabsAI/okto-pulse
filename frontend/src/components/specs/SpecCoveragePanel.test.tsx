import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { SpecCoverageResponse } from './specCoverageTypes';
const api = vi.hoisted(() => ({ getSpecCoverage: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('@/components/code-traceability/DeliveryEvidencePanel', () => ({ DeliveryEvidencePanel: () => <div>Unified implementation checks</div> }));
import { SpecCoveragePanel } from './SpecCoveragePanel';

const base: SpecCoverageResponse = {
  view: 'coverage', subject_ref: 'spec:spec', edition: 1, authority: 'informational', data_source: 'composed',
  projection_freshness: { state: 'incomplete', graph_generation: 'one', source_checkpoint: 'source',
    projection_checkpoint: null, checked_at: '2026-10-01T00:00:00Z' },
  completeness: { complete_for_scope: false, truncated: false, limitations: [] },
  structure: { authority: 'spec_coverage_summary', interpretation: 'planning_links_not_delivery_proof',
    complete_for_scope: true, summary: { ac_covered: 1, ac_total: 1, fr_covered: 0, fr_total: 1,
      scenarios_linked: 1, scenarios_total: 1, brs_linked: 0, brs_total: 0,
      contracts_linked: 0, contracts_total: 0, trs_linked: 0, trs_total: 0,
      decisions_planned: 0, decisions_total: 0, irs_linked: 0, irs_total: 0,
      ors_linked: 0, ors_total: 0 }, graph: { state: 'observed', complete_for_scope: false,
      expected_nodes: 3, observed_nodes: 2, missing_nodes: 1, missing_relations: 1 } },
  delivery: { state: 'available', authority: 'evaluate_delivery_coverage', complete_for_scope: true,
    counts: { obligations: 2, implementation_proven: 0, verification_proven: 0, observed_obligations: 2, decisions: 0, decisions_verified: 0 },
    blockers: [], rejected_record_refs: [], interpretation: 'informational' }, items: [], next_cursor: null,
};
function page(title: string, next: string | null = null): SpecCoverageResponse {
  return { ...base, next_cursor: next, items: [{ kind: 'structure_node', node_type: 'spec', subject_ref: title, observation: 'observed' }] };
}
beforeEach(() => { vi.clearAllMocks(); api.getSpecCoverage.mockResolvedValue(base); });

it('shows the shared pulse animation while coverage is pending and removes it when loaded', async () => {
  let finish!: (value: SpecCoverageResponse) => void;
  api.getSpecCoverage.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  const loader = screen.getByRole('status', { name: 'Loading coverage…' });
  expect(loader.querySelector('.pulse-loader__trace')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Reload coverage' })).toBeDisabled();
  await act(async () => finish(base));
  expect(screen.queryByRole('status', { name: 'Loading coverage…' })).not.toBeInTheDocument();
  expect(screen.getByText('67%')).toBeInTheDocument();
});

it('keeps linked Test Cards separate from passing proof and graph absence', async () => {
  api.getSpecCoverage.mockResolvedValue(page('Unverified requirement'));
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findByText('67%')).toBeInTheDocument();
  expect(screen.getAllByText('0%')).toHaveLength(2);
  expect(screen.queryByText('Unverified requirement')).not.toBeInTheDocument();
  expect(screen.getByText('Technical diagnostics').closest('details')).not.toHaveAttribute('open');
  fireEvent.click(screen.getByRole('tab', { name: 'Planning' }));
  expect(screen.getByRole('progressbar', { name: 'Scenarios → Test Cards' })).toHaveAttribute('aria-valuenow', '1');
  expect(screen.getByText(/does not approve a gate/)).toBeInTheDocument();
});

it('paginates a pinned observation without adding whole-scope counts', async () => {
  api.getSpecCoverage.mockResolvedValueOnce(page('first', 'cursor')).mockResolvedValueOnce(page('second'));
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  fireEvent.click(await screen.findByText('Technical diagnostics'));
  fireEvent.click(screen.getByRole('button', { name: 'Load more diagnostics' }));
  expect(await screen.findByText(/spec: second/)).toBeInTheDocument();
  expect(screen.getByText(/spec: first/)).toBeInTheDocument();
  expect(api.getSpecCoverage.mock.calls[1][2]).toEqual({ limit: 200, cursor: 'cursor' });
  expect(screen.getAllByText('0 of 2')).toHaveLength(2);
});

it.each([403, 409, 503])('clears earlier data on HTTP %s', async status => {
  api.getSpecCoverage.mockResolvedValueOnce(page('first', 'cursor')).mockRejectedValueOnce(new AuthenticatedFetchError({ status, message: 'failure' }));
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  fireEvent.click(await screen.findByText('Technical diagnostics'));
  fireEvent.click(screen.getByRole('button', { name: 'Load more diagnostics' }));
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.queryByText(/spec: first/)).not.toBeInTheDocument();
});

it('aborts on Spec changes and ignores a late response', async () => {
  let resolveOld!: (value: SpecCoverageResponse) => void;
  api.getSpecCoverage.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; })).mockResolvedValueOnce(page('new-spec'));
  const view = render(<SpecCoveragePanel boardId="board" specId="old" revision="1" onOpenSection={vi.fn()} />);
  const signal = api.getSpecCoverage.mock.calls[0][3] as AbortSignal;
  view.rerender(<SpecCoveragePanel boardId="board" specId="new" revision="1" onOpenSection={vi.fn()} />);
  expect(signal.aborted).toBe(true);
  expect(await screen.findByText(/spec: new-spec/)).toBeInTheDocument();
  await act(async () => resolveOld(page('old-spec')));
  expect(screen.queryByText(/spec: old-spec/)).not.toBeInTheDocument();
  view.unmount();
  expect((api.getSpecCoverage.mock.calls[1][3] as AbortSignal).aborted).toBe(true);
});

it('keeps restricted proof unknown rather than zero', async () => {
  api.getSpecCoverage.mockResolvedValue({ ...base, delivery: { ...base.delivery, state: 'restricted',
    counts: { obligations: null, implementation_proven: null, verification_proven: null, observed_obligations: null } } });
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findAllByText('Unavailable')).toHaveLength(3);
  expect(screen.queryByText('0%')).not.toBeInTheDocument();
});

it('correction only opens authorized domain editing and never writes', async () => {
  const navigate = vi.fn();
  const view = render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={navigate} />);
  fireEvent.click(screen.getByRole('tab', { name: 'Planning' }));
  await screen.findByText('Functional requirements → rules');
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

it('merges implementation and verification navigation into the original checks', async () => {
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  await screen.findByText('67%');
  expect(screen.getAllByRole('tab')).toHaveLength(3);
  expect(screen.queryByRole('tab', { name: 'Verification' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Review verification' }));
  expect(screen.getByRole('tab', { name: 'Implementation' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByText('Unified implementation checks')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: 'Overview' }));
  fireEvent.click(screen.getByRole('button', { name: 'Review planning gaps' }));
  expect(screen.getByRole('tab', { name: 'Planning' })).toHaveAttribute('aria-selected', 'true');
  expect(api.getSpecCoverage).toHaveBeenCalledTimes(1);
});

it('preserves implementation access without requiring broader summary permissions', () => {
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" canReadSummary={false} onOpenSection={vi.fn()} />);
  expect(screen.getByText('Unified implementation checks')).toBeInTheDocument();
  expect(screen.getAllByRole('tab')).toHaveLength(1);
  expect(api.getSpecCoverage).not.toHaveBeenCalled();
});

it('does not expose implementation without its permission', async () => {
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" canReadImplementation={false} onOpenSection={vi.fn()} />);
  await screen.findByText('67%');
  expect(screen.queryByRole('tab', { name: 'Implementation' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Review implementation' })).toBeDisabled();
});

it('shows empty scopes without claiming full coverage', async () => {
  api.getSpecCoverage.mockResolvedValue({ ...base,
    structure: { ...base.structure, summary: Object.fromEntries(Object.keys(base.structure.summary!).map(key => [key, 0])) },
    delivery: { ...base.delivery, counts: { obligations: 0, implementation_proven: 0, verification_proven: 0, observed_obligations: 0 } },
  });
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findAllByText('No items')).toHaveLength(3);
  expect(screen.queryByText('100%')).not.toBeInTheDocument();
});

it('does not derive scores from partial counts', async () => {
  api.getSpecCoverage.mockResolvedValue({ ...base,
    structure: { ...base.structure, complete_for_scope: false },
    delivery: { ...base.delivery, complete_for_scope: false },
  });
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findAllByText('Unavailable')).toHaveLength(4);
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});
