import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { SpecCoverageResponse } from './specCoverageTypes';
import { persistedProjection, metric } from '@/components/code-traceability/__tests__/readinessFixture';
const api = vi.hoisted(() => ({ getSpecCoverage: vi.fn(), getPersistedReadinessProjection: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('@/components/code-traceability/DeliveryEvidencePanel', () => ({ DeliveryEvidencePanel: ({ focusExceptionId }: { focusExceptionId?: string }) => <div>Unified implementation checks{focusExceptionId && <span>Exception: {focusExceptionId}</span>}</div> }));
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
function proof(implementation = 0, verification = 0, total = 2, boardId = 'board', specId = 'spec') {
  const value = persistedProjection();
  value.scope.board_id = boardId;
  value.scope.spec_id = specId;
  value.summaries = [value.summaries[0]];
  const phase = (satisfied: number) => ({ ...metric(total ? satisfied / total * 100 : 0, satisfied),
    total, applicable: total, unresolved: total - satisfied,
    counts_by_state: { ...metric(0, 0).counts_by_state, satisfied, missing: total - satisfied } });
  value.summaries[0].implementation = phase(implementation);
  value.summaries[0].verification = phase(verification);
  return value;
}
beforeEach(() => {
  vi.clearAllMocks();
  api.getSpecCoverage.mockResolvedValue(base);
  api.getPersistedReadinessProjection.mockImplementation((boardId: string, specId: string) => Promise.resolve(proof(0, 0, 2, boardId, specId)));
});

it('keeps planning and independent proof visible when graph details are unavailable', async () => {
  api.getSpecCoverage.mockResolvedValue({ ...base, data_source: 'relational',
    structure: { ...base.structure, graph: { state: 'unavailable', complete_for_scope: false,
      expected_nodes: null, observed_nodes: null, missing_nodes: null, missing_relations: null } },
  });
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findByText(/Graph details are temporarily unavailable/)).toBeVisible();
  expect(screen.getByText('67%')).toBeVisible();
  expect(screen.getAllByText('0%')).toHaveLength(2);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('tab', { name: 'Planning' }));
  expect(screen.getByRole('progressbar', { name: 'Scenarios → Test Cards' })).toBeVisible();
  expect(api.getSpecCoverage).toHaveBeenCalledTimes(1);
});

it('explains a coverage timeout and retains access to Implementation', async () => {
  api.getSpecCoverage.mockRejectedValue(new AuthenticatedFetchError({ status: 504, message: 'timeout' }));
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Coverage took too long to load.');
  fireEvent.click(screen.getByRole('tab', { name: 'Implementation' }));
  expect(screen.getByText('Unified implementation checks')).toBeVisible();
});

it('opens the native implementation subtab for an exact exception and retains read authority', async () => {
  const view = render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" focusExceptionId="waiver" onOpenSection={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole('tab', { name: 'Implementation' })).toHaveAttribute('aria-selected', 'true'));
  expect(screen.getByText('Exception: waiver')).toBeVisible();
  view.rerender(<SpecCoveragePanel boardId="board" specId="spec" revision="1" focusExceptionId="waiver" onOpenSection={vi.fn()} canReadImplementation={false} />);
  expect(screen.queryByText('Exception: waiver')).not.toBeInTheDocument();
});

it('displays inherited BR planning coverage without granting implementation or verification credit', async () => {
  api.getSpecCoverage.mockResolvedValue({ ...base,
    structure: { ...base.structure, summary: { ...base.structure.summary,
      fr_covered: 1, brs_linked: 1, brs_total: 1 } },
  });
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findByText('100%')).toBeInTheDocument();
  expect(screen.getAllByText('0%')).toHaveLength(2);
  fireEvent.click(screen.getByRole('tab', { name: 'Planning' }));
  expect(screen.getByRole('progressbar', { name: 'Rules → Cards' })).toHaveAttribute('aria-valuenow', '1');
});

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
  api.getPersistedReadinessProjection.mockRejectedValue(new AuthenticatedFetchError({ status: 403, message: 'Restricted proof' }));
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

it('updates factual delivery progress on a delivery revision without a new Spec edition', async () => {
  const view = render(<SpecCoveragePanel boardId="board" specId="spec" revision="delivery-1" onOpenSection={vi.fn()} />);
  expect(await screen.findByRole('progressbar', { name: 'Implementation proof' })).toHaveAttribute('aria-valuenow', '0');
  api.getSpecCoverage.mockResolvedValue({ ...base, delivery: { ...base.delivery,
    counts: { ...base.delivery.counts, implementation_proven: 2, verification_proven: 1 },
    blockers: ['delivery_test_result_missing'],
  } });
  api.getPersistedReadinessProjection.mockResolvedValue(proof(2, 1));
  view.rerender(<SpecCoveragePanel boardId="board" specId="spec" revision="delivery-2" onOpenSection={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole('progressbar', { name: 'Implementation proof' })).toHaveAttribute('aria-valuenow', '2'));
  expect(screen.getByRole('progressbar', { name: 'Verification proof' })).toHaveAttribute('aria-valuenow', '1');
  expect(screen.getByText('100%')).toBeVisible();
  expect(screen.getByText('50%')).toBeVisible();
  expect(screen.getByText(/Edition 1/)).toBeVisible();
  expect(api.getSpecCoverage).toHaveBeenCalledTimes(2);
  expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
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
  fireEvent.click(await screen.findByRole('button', { name: 'Review planning gaps' }));
  expect(screen.getByRole('tab', { name: 'Planning' })).toHaveAttribute('aria-selected', 'true');
  expect(api.getSpecCoverage).toHaveBeenCalledTimes(2);
});

it('reloads both structural coverage and persisted proof when explicitly requested', async () => {
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findByRole('progressbar', { name: 'Implementation proof' })).toHaveAttribute('aria-valuenow', '0');
  api.getPersistedReadinessProjection.mockResolvedValue(proof(2, 1));
  fireEvent.click(screen.getByRole('button', { name: 'Reload coverage' }));
  await waitFor(() => expect(screen.getByRole('progressbar', { name: 'Implementation proof' })).toHaveAttribute('aria-valuenow', '2'));
  expect(screen.getByRole('progressbar', { name: 'Verification proof' })).toHaveAttribute('aria-valuenow', '1');
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
  expect(api.getSpecCoverage).toHaveBeenCalledTimes(2);
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
    delivery: { ...base.delivery, counts: { obligations: 0, implementation_proven: 0, verification_proven: 0, observed_obligations: 0, decisions: 0, decisions_verified: 0 } },
  });
  const empty = proof(0, 0, 0);
  for (const phase of [empty.summaries[0].implementation, empty.summaries[0].verification]) {
    phase.status = 'not_applicable'; phase.percent = null;
  }
  api.getPersistedReadinessProjection.mockResolvedValue(empty);
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findAllByText('No items')).toHaveLength(2);
  expect(await screen.findAllByText('Not applicable')).toHaveLength(2);
  expect(screen.queryByText('100%')).not.toBeInTheDocument();
});

it('does not derive scores from partial counts', async () => {
  const incomplete = proof();
  incomplete.summaries[0].population_known = false;
  for (const phase of [incomplete.summaries[0].implementation, incomplete.summaries[0].verification]) {
    phase.status = 'unknown'; phase.percent = null; phase.applicable = null;
  }
  api.getPersistedReadinessProjection.mockResolvedValue(incomplete);
  api.getSpecCoverage.mockResolvedValue({ ...base,
    structure: { ...base.structure, complete_for_scope: false },
    delivery: { ...base.delivery, complete_for_scope: false },
  });
  render(<SpecCoveragePanel boardId="board" specId="spec" revision="1" onOpenSection={vi.fn()} />);
  expect(await screen.findAllByText('Unavailable')).toHaveLength(2);
  expect(await screen.findAllByText('Unable to evaluate')).toHaveLength(2);
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});
