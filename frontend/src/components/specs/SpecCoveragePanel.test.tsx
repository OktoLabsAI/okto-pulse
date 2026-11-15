import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { SpecCoverageResponse } from './specCoverageTypes';
import { persistedProjection } from '@/components/code-traceability/__tests__/readinessFixture';
import { deliverySummary } from '@/components/code-traceability/__tests__/deliverySummaryFixture';
import { coverageHelp } from './coverageHelp';
import { HELP_SECTION_IDS, CONTEXTUAL_HELP_EVENT } from '@/components/help/contextualHelp';
const api = vi.hoisted(() => ({ getSpecCoverage: vi.fn(), getPersistedReadinessProjection: vi.fn(), getDeliverySummary: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('@/components/code-traceability/DeliveryEvidencePanel', () => ({
  DeliveryEvidenceDetails: ({ focusExceptionId }: { focusExceptionId?: string }) => <div>Unified implementation checks{focusExceptionId && <span>Exception: {focusExceptionId}</span>}</div>,
}));
import { SpecCoveragePanel } from './SpecCoveragePanel';

function source(specId = 's', edition = 1, name = 'Observed requirement', cursor: string | null = null): SpecCoverageResponse {
  return { view: 'coverage', subject_ref: `spec:${specId}`, edition, authority: 'informational', data_source: 'relational',
    projection_freshness: { state: 'unavailable', graph_generation: null, source_checkpoint: 'source-1', projection_checkpoint: null, checked_at: '2026-11-06T00:00:00Z' },
    completeness: { complete_for_scope: false, truncated: false, limitations: [] },
    structure: { authority: 'spec_coverage_summary', interpretation: 'planning_links_not_delivery_proof', complete_for_scope: true,
      summary: { ac_covered: 1, ac_total: 1, fr_covered: 0, fr_total: 1, scenarios_linked: 1, scenarios_total: 1,
        brs_linked: 0, brs_total: 0, contracts_linked: 0, contracts_total: 0, trs_linked: 0, trs_total: 0,
        decisions_planned: 0, decisions_total: 0, irs_linked: 0, irs_total: 0, ors_linked: 0, ors_total: 0 },
      graph: { state: 'unavailable', complete_for_scope: false, expected_nodes: null, observed_nodes: null, missing_nodes: null, missing_relations: null } },
    delivery: { state: 'available', authority: 'evaluate_delivery_coverage', complete_for_scope: true,
      counts: { obligations: 8, implementation_proven: 0, verification_proven: 0, observed_obligations: 8, decisions: 0, decisions_verified: 0 }, blockers: [], rejected_record_refs: [], interpretation: 'informational' },
    items: [{ kind: 'structure_node', node_type: 'requirement', subject_ref: name, observation: 'not_found_in_projection' }], next_cursor: cursor };
}
const props = { boardId: 'b', specId: 's', edition: 1, revision: '1', onOpenSection: vi.fn() };
beforeEach(() => {
  vi.resetAllMocks();
  api.getSpecCoverage.mockImplementation((_board, spec) => Promise.resolve(source(spec)));
  api.getPersistedReadinessProjection.mockImplementation((board, spec) => {
    const p = persistedProjection(); p.scope.board_id = board; p.scope.spec_id = spec; return Promise.resolve(p);
  });
  api.getDeliverySummary.mockImplementation((board, spec) => {
    const p = deliverySummary(); p.scope.board_id = board; p.scope.spec_id = spec; p.scope.spec_edition = 1; return Promise.resolve(p);
  });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });
const show = () => fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
const hide = () => fireEvent.click(screen.getByRole('button', { name: 'Hide details' }));

it('renders independent planning and persisted proof while optional graph observations are unavailable', async () => {
  render(<SpecCoveragePanel {...props} />);
  expect(await screen.findByText('67%')).toBeVisible();
  expect(screen.getByText('100%')).toBeVisible();
  expect(screen.getByText('75%')).toBeVisible();
  expect(screen.queryByText('0%')).not.toBeInTheDocument();
  expect(screen.getByText(/Graph details are temporarily unavailable/)).toBeVisible();
  expect(screen.getByText(/does not approve a gate/)).toBeVisible();
  expect(screen.queryByRole('tab')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /reload|refresh/i })).not.toBeInTheDocument();
  expect(api.getSpecCoverage.mock.calls[0][2]).toEqual({ graph_mode: 'summary', limit: 200, cursor: undefined });
});

it('renders delivery proof before a slow planning query completes', async () => {
  api.getSpecCoverage.mockReturnValue(new Promise(() => {}));
  render(<SpecCoveragePanel {...props} />);
  expect(await screen.findByText('75%')).toBeVisible();
  expect(screen.getByRole('status', { name: 'Loading coverage…' })).toBeVisible();
  expect(within(screen.getByRole('region', { name: 'Delivery status' })).getByText('Delivered:')).toBeVisible();
});

it('loads one detail observation per opening, never on hide, and resets the cursor across modes', async () => {
  api.getSpecCoverage.mockResolvedValue(source('s', 1, 'first', 'opaque'));
  const view = render(<SpecCoveragePanel {...props} />);
  await screen.findByText('67%'); show();
  await waitFor(() => expect(api.getSpecCoverage).toHaveBeenCalledTimes(2));
  expect(api.getSpecCoverage.mock.calls[1][2]).toEqual({ graph_mode: 'detail', limit: 200, cursor: undefined });
  hide(); expect(api.getSpecCoverage).toHaveBeenCalledTimes(2);
  show(); await waitFor(() => expect(api.getSpecCoverage).toHaveBeenCalledTimes(3));
  view.rerender(<SpecCoveragePanel {...props} revision="2" />);
  await waitFor(() => expect(api.getSpecCoverage).toHaveBeenCalledTimes(4));
  expect(api.getSpecCoverage.mock.calls[3][2].graph_mode).toBe('detail');
  expect(api.getSpecCoverage.mock.calls[3][2].cursor).toBeUndefined();
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
  expect(api.getDeliverySummary).toHaveBeenCalledTimes(2);
  hide(); view.rerender(<SpecCoveragePanel {...props} revision="3" />);
  await waitFor(() => expect(api.getSpecCoverage).toHaveBeenCalledTimes(5));
  expect(api.getSpecCoverage.mock.calls[4][2].graph_mode).toBe('summary');
});

it('retains scoped previous planning on refresh timeout without presenting it as current', async () => {
  const view = render(<SpecCoveragePanel {...props} />);
  await screen.findByText('67%');
  api.getSpecCoverage.mockRejectedValue(new AuthenticatedFetchError({ status: 504, message: 'timeout' }));
  view.rerender(<SpecCoveragePanel {...props} revision="2" />);
  expect(await screen.findByText(/Coverage took too long/)).toBeVisible();
  expect(screen.getByText('67%')).toBeVisible();
  expect(screen.getByText('Previous planning data')).toBeVisible();
  expect(screen.queryByText('Planning data is current')).not.toBeInTheDocument();
  expect(screen.getByText('75%')).toBeVisible();
});

it.each([401, 403, 404, 409])('clears planning rather than retaining unauthorized/changed data on %s', async status => {
  const view = render(<SpecCoveragePanel {...props} />);
  await screen.findByText('67%');
  api.getSpecCoverage.mockRejectedValue(new AuthenticatedFetchError({ status, message: 'changed' }));
  view.rerender(<SpecCoveragePanel {...props} revision="2" />);
  await screen.findByRole('alert');
  expect(screen.queryByText('67%')).not.toBeInTheDocument();
  expect(screen.queryByText('Previous planning data')).not.toBeInTheDocument();
});

it.each(['board', 'spec', 'edition', 'permission'])('aborts and clears old responses when %s changes', async changed => {
  let finish!: (v: SpecCoverageResponse) => void;
  api.getSpecCoverage.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const view = render(<SpecCoveragePanel {...props} />);
  const signal = api.getSpecCoverage.mock.calls[0][3] as AbortSignal;
  const next = { ...props, ...(changed === 'board' ? { boardId: 'new-board' } : changed === 'spec' ? { specId: 'new-spec' }
    : changed === 'edition' ? { edition: 2 } : { canReadSummary: false }) };
  api.getSpecCoverage.mockResolvedValue(source(next.specId, next.edition, 'new'));
  view.rerender(<SpecCoveragePanel {...next} />);
  expect(signal.aborted).toBe(true);
  await act(async () => finish(source('s', 1, 'old')));
  await act(async () => show()); if (changed !== 'permission') fireEvent.click(screen.getByText('Technical diagnostics'));
  expect(screen.queryByText(/requirement: old/)).not.toBeInTheDocument();
});

it('blocks and discards observations while Board access is being rechecked', async () => {
  const view = render(<SpecCoveragePanel {...props} />); await screen.findByText('75%');
  view.rerender(<SpecCoveragePanel {...props} accessPending />);
  expect(screen.queryByText('75%')).not.toBeInTheDocument();
  expect(screen.getByRole('status', { name: 'Checking Coverage access…' })).toBeVisible();
  expect((api.getPersistedReadinessProjection.mock.calls[0][3] as AbortSignal).aborted).toBe(true);
});

it('waits for initial modal synchronization without resetting an active Details view on later refresh', async () => {
  const view = render(<SpecCoveragePanel {...props} initialReadPending />);
  expect(screen.getByRole('status', { name: 'Loading the current Spec snapshot…' })).toBeVisible();
  expect(api.getSpecCoverage).not.toHaveBeenCalled();
  expect(api.getPersistedReadinessProjection).not.toHaveBeenCalled();
  expect(api.getDeliverySummary).not.toHaveBeenCalled();
  view.rerender(<SpecCoveragePanel {...props} revision="2" />);
  await screen.findByText('67%');
  expect(api.getSpecCoverage).toHaveBeenCalledOnce();
  show(); await waitFor(() => expect(api.getSpecCoverage).toHaveBeenCalledTimes(2));
  view.rerender(<SpecCoveragePanel {...props} revision="2" initialReadPending />);
  expect(screen.getByRole('button', { name: 'Hide details' })).toBeVisible();
  expect(api.getSpecCoverage).toHaveBeenCalledTimes(2);
  view.rerender(<SpecCoveragePanel {...props} revision="3" />);
  await waitFor(() => expect(api.getSpecCoverage).toHaveBeenCalledTimes(3));
  expect(api.getSpecCoverage.mock.calls[2][2].graph_mode).toBe('detail');
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2);
});

it('puts unresolved requirements first and keeps full long requirement text available on demand', async () => {
  const p = persistedProjection();
  const base = { card_id: null, kind: 'obligation' as const, semantic_sha256: 'a'.repeat(64), spec_edition: 1,
    contribution_card_ids: [], implementation_state: 'satisfied', verification_state: 'satisfied',
    implementation_reason_codes: [], verification_reason_codes: [] };
  p.units = [{ ...base, ref: 'fr:complete' }, { ...base, ref: 'fr:pending', verification_state: 'missing' }];
  api.getPersistedReadinessProjection.mockResolvedValue(p);
  const long = `FR-2: ${'A clear requirement with a long explanation. '.repeat(6)}`.trim();
  render(<SpecCoveragePanel {...props} requirementTitles={{ complete: 'FR-1: Complete work', pending: long }} />);
  const table = await screen.findByRole('region', { name: 'Requirement delivery matrix' });
  const rows = within(table).getAllByRole('row').slice(1);
  expect(rows[0]).toHaveTextContent('FR-2:');
  expect(rows[1]).toHaveTextContent('FR-1: Complete work');
  const fullText = within(rows[0]).getByText(long, { selector: 'p' });
  expect(fullText.closest('details')).not.toHaveAttribute('open');
  fireEvent.click(within(rows[0]).getByText(`${long.slice(0, 140).trimEnd()}…`));
  expect(fullText).toBeVisible();
  expect(api.getSpecCoverage).toHaveBeenCalledOnce();
});

it('paginates only the current detail observation and rejects a changed source page', async () => {
  api.getSpecCoverage.mockResolvedValueOnce(source()).mockResolvedValueOnce(source('s', 1, 'first', 'cursor'))
    .mockResolvedValueOnce({ ...source('s', 1, 'foreign'), projection_freshness: { ...source().projection_freshness, source_checkpoint: 'changed' } });
  render(<SpecCoveragePanel {...props} />); await screen.findByText('67%'); show();
  fireEvent.click(screen.getByText('Technical diagnostics'));
  fireEvent.click(await screen.findByRole('button', { name: 'Load more diagnostics' }));
  expect(await screen.findByText(/source or graph changed/)).toBeVisible();
  expect(screen.queryByText(/requirement: foreign/)).not.toBeInTheDocument();
  expect(api.getSpecCoverage.mock.calls[2][2]).toEqual({ graph_mode: 'detail', limit: 200, cursor: 'cursor' });
});

it('does not poll or retry unavailable or updating summary facts', async () => {
  vi.useFakeTimers();
  const p = persistedProjection(); p.projection.freshness = 'updating';
  api.getPersistedReadinessProjection.mockResolvedValue(p);
  render(<SpecCoveragePanel {...props} />);
  await act(async () => { await Promise.resolve(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  fireEvent(window, new Event('focus')); fireEvent(document, new Event('visibilitychange'));
  expect(api.getSpecCoverage).toHaveBeenCalledOnce();
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledOnce();
  expect(api.getDeliverySummary).toHaveBeenCalledOnce();
});

it('retains the detailed Implementation view and exact exception navigation only when authorized', async () => {
  const view = render(<SpecCoveragePanel {...props} focusExceptionId="waiver" />);
  expect(await screen.findByText('Exception: waiver')).toBeVisible();
  await screen.findByText('67%');
  expect(api.getSpecCoverage.mock.calls[0][2].graph_mode).toBe('detail');
  hide(); expect(api.getSpecCoverage).toHaveBeenCalledOnce();
  expect(screen.queryByText('Exception: waiver')).not.toBeInTheDocument();
  await act(async () => view.rerender(<SpecCoveragePanel {...props} focusExceptionId="waiver" canReadImplementation={false} />));
  expect(screen.queryByText('Exception: waiver')).not.toBeInTheDocument();
  expect(screen.queryByText('75%')).not.toBeInTheDocument();
});

it('links the failed verification and next action to the precise requirement row', async () => {
  const p = persistedProjection();
  p.units = [{ card_id: null, kind: 'obligation', ref: 'fr:login', semantic_sha256: 'a'.repeat(64), spec_edition: 1,
    contribution_card_ids: ['task'], implementation_state: 'satisfied', verification_state: 'failed',
    implementation_reason_codes: [], verification_reason_codes: ['verification_failed'] }];
  api.getPersistedReadinessProjection.mockResolvedValue(p);
  render(<SpecCoveragePanel {...props} requirementTitles={{ login: 'FR-1: Sign in securely' }} />);
  const row = (await screen.findByText('FR-1: Sign in securely')).closest('tr')!;
  expect(within(row).getByText('Implemented')).toBeVisible();
  expect(within(row).getByText('Failed')).toBeVisible();
  expect(within(row).getByText('The check for this delivered version failed.')).toBeVisible();
  expect(within(row).getByText(/fix its cause, then verify/)).toBeVisible();
});

it('keeps no-summary access scoped and opens implementation only on demand', async () => {
  render(<SpecCoveragePanel {...props} canReadSummary={false} />);
  expect(await screen.findByText('75%')).toBeVisible();
  expect(api.getSpecCoverage).not.toHaveBeenCalled(); expect(api.getDeliverySummary).not.toHaveBeenCalled();
  show(); expect(screen.getByText('Unified implementation checks')).toBeVisible();
});

it('routes contextual help to the registered central English guide', async () => {
  const listener = vi.fn(); window.addEventListener(CONTEXTUAL_HELP_EVENT, listener);
  render(<SpecCoveragePanel {...props} />);
  await screen.findByText('67%');
  fireEvent.click(screen.getByRole('button', { name: 'How Coverage works' }));
  expect(listener.mock.calls[0][0].detail.sectionId).toBe('spec-coverage');
  expect(HELP_SECTION_IDS).toContain('spec-coverage');
  expect(coverageHelp).toContain('The application creates');
  expect(coverageHelp).toContain('submission receipt automatically');
  expect(coverageHelp).toContain('**Previous data**');
  window.removeEventListener(CONTEXTUAL_HELP_EVENT, listener);
});

it.each([false, true])('loads a manual requirement page and rejects generation changes (%s)', async changed => {
  const first = persistedProjection();
  const row = { card_id: null, kind: 'obligation' as const, ref: 'fr:first', semantic_sha256: 'a'.repeat(64),
    spec_edition: 1, contribution_card_ids: [], implementation_state: 'satisfied', verification_state: 'missing',
    implementation_reason_codes: [], verification_reason_codes: ['verification_not_run'] };
  first.units = [row]; first.page = { offset: 0, limit: 1, total: 2, has_more: true, next_cursor: 'cursor-1' };
  const next = structuredClone(first); next.units = [{ ...row, ref: 'fr:second' }];
  next.page = { offset: 1, limit: 1, total: 2, has_more: false, next_cursor: null };
  if (changed) next.projection.generation_id = 'another-generation';
  api.getPersistedReadinessProjection.mockResolvedValueOnce(first).mockResolvedValueOnce(next);
  render(<SpecCoveragePanel {...props} requirementTitles={{ first: 'First requirement', second: 'Second requirement' }} />);
  await screen.findByText('First requirement');
  fireEvent.click(screen.getByRole('button', { name: 'Load more requirements' }));
  if (changed) {
    expect(await screen.findByText(/Requirement details changed/)).toBeVisible();
    expect(screen.queryByText('Second requirement')).not.toBeInTheDocument();
    expect(screen.queryByText('Delivery data is current')).not.toBeInTheDocument();
  } else {
    expect(await screen.findByText('Second requirement')).toBeVisible();
    expect(screen.getByText('First requirement')).toBeVisible();
  }
  expect(api.getPersistedReadinessProjection.mock.calls[1][2]).toEqual({ unitLimit: 250, cursor: 'cursor-1' });
  expect(api.getSpecCoverage).toHaveBeenCalledOnce();
});
