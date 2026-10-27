import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { SpecCoveragePanel } from '../SpecCoveragePanel';
import type { SpecCoverageResponse } from '../specCoverageTypes';
import { persistedProjection } from '@/components/code-traceability/__tests__/readinessFixture';

const api = vi.hoisted(() => ({
  getSpecCoverage: vi.fn(),
  getPersistedReadinessProjection: vi.fn(),
}));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('@/components/code-traceability/DeliveryEvidencePanel', () => ({
  DeliveryEvidencePanel: () => <div>Persisted coverage table</div>,
}));

function sourceCoverage(): SpecCoverageResponse {
  return {
    view: 'coverage', subject_ref: 'spec:s', edition: 1, authority: 'informational',
    data_source: 'relational',
    projection_freshness: { state: 'unknown', graph_generation: null,
      source_checkpoint: 'source', projection_checkpoint: null, checked_at: '2026-10-25T00:00:00Z' },
    completeness: { complete_for_scope: false, truncated: false, limitations: [] },
    structure: { authority: 'spec_coverage_summary', interpretation: 'planning_links_not_delivery_proof',
      complete_for_scope: false, summary: null,
      graph: { state: 'unavailable', complete_for_scope: false, expected_nodes: null,
        observed_nodes: null, missing_nodes: null, missing_relations: null } },
    // The older structural query reports zero; proof meters must use the
    // generation read shared with the Coverage by obligation table.
    delivery: { state: 'available', authority: 'evaluate_delivery_coverage',
      complete_for_scope: true, counts: { obligations: 9, implementation_proven: 0,
        verification_proven: 0, observed_obligations: 9, decisions: 0,
        decisions_verified: 0 }, blockers: [], rejected_record_refs: [], interpretation: '' },
    items: [], next_cursor: null,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getSpecCoverage.mockResolvedValue(sourceCoverage());
  const value = persistedProjection();
  value.summaries[0].implementation.percent = 88;
  value.summaries[0].implementation.satisfied = 8;
  value.summaries[0].implementation.applicable = 9;
  value.summaries[0].verification.percent = 50;
  api.getPersistedReadinessProjection.mockResolvedValue(value);
});
afterEach(cleanup);

it('uses the persisted Spec generation for Overview proof percentages', async () => {
  render(<SpecCoveragePanel boardId="b" specId="s" revision="1" onOpenSection={() => {}} />);
  const implementation = await screen.findByText('Implementation proof');
  await waitFor(() => expect(within(implementation.closest('div')!).getByText('88%')).toBeVisible());
  const verification = screen.getByText('Verification proof');
  expect(within(verification.closest('div')!).getByText('50%')).toBeVisible();
  expect(screen.getByText(/Generation generation-1/)).toBeVisible();
  expect(api.getSpecCoverage).toHaveBeenCalledOnce();
  expect(api.getPersistedReadinessProjection).toHaveBeenCalledOnce();
});

it('reloads persisted proof with structural coverage after calculation becomes available', async () => {
  const unavailable = persistedProjection();
  unavailable.projection.freshness = 'unavailable';
  unavailable.projection.generation_id = null;
  unavailable.summaries = [];
  const recovered = persistedProjection();
  recovered.summaries[0].implementation.percent = 75;
  api.getPersistedReadinessProjection.mockResolvedValueOnce(unavailable).mockResolvedValueOnce(recovered);
  render(<SpecCoveragePanel boardId="b" specId="s" revision="1" onOpenSection={() => {}} />);
  await screen.findByText('Calculation unavailable');
  fireEvent.click(screen.getByRole('button', { name: 'Reload coverage' }));
  await waitFor(() => expect(api.getPersistedReadinessProjection).toHaveBeenCalledTimes(2));
  const implementation = screen.getByText('Implementation proof');
  await waitFor(() => expect(within(implementation.closest('div')!).getByText('75%')).toBeVisible());
  expect(api.getSpecCoverage).toHaveBeenCalledTimes(2);
});

it('does not invoke the structural coverage query on the Implementation tab', () => {
  render(<SpecCoveragePanel boardId="b" specId="s" revision="1"
    canReadSummary={false} canReadImplementation onOpenSection={() => {}} />);
  expect(screen.getByText('Persisted coverage table')).toBeVisible();
  expect(api.getSpecCoverage).not.toHaveBeenCalled();
  expect(api.getPersistedReadinessProjection).not.toHaveBeenCalled();
});

it('shows waiting without inventing zero before the first generation', async () => {
  const value = persistedProjection();
  value.projection.freshness = 'pending_initial';
  value.projection.generation_id = null;
  value.summaries = [];
  api.getPersistedReadinessProjection.mockResolvedValue(value);
  render(<SpecCoveragePanel boardId="b" specId="s" revision="1" onOpenSection={() => {}} />);
  const implementation = await screen.findByText('Implementation proof');
  await waitFor(() => expect(within(implementation.closest('div')!).getByText('Waiting for calculation')).toBeVisible());
  const verification = screen.getByText('Verification proof');
  expect(within(verification.closest('div')!).getByText('Waiting for calculation')).toBeVisible();
  expect(within(implementation.closest('div')!).queryByText('0%')).not.toBeInTheDocument();
});

it('labels a nonapplicable persisted phase separately from missing proof', async () => {
  const value = persistedProjection();
  value.summaries[0].implementation.status = 'not_applicable';
  value.summaries[0].implementation.percent = null;
  api.getPersistedReadinessProjection.mockResolvedValue(value);
  render(<SpecCoveragePanel boardId="b" specId="s" revision="1" onOpenSection={() => {}} />);
  const implementation = await screen.findByText('Implementation proof');
  await waitFor(() => expect(within(implementation.closest('div')!).getByText('Not applicable')).toBeVisible());
});
