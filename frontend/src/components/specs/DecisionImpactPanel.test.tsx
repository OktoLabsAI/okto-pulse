import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { DecisionImpactResponse } from './decisionImpactTypes';
import type { Spec } from '@/types';
const api = vi.hoisted(() => ({ getDecisionImpact: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
vi.mock('@/hooks/useCognitivePendingBadges', () => ({ useCognitivePendingBadges: () => ({ badges: {} }) }));
import { DecisionImpactPanel } from './DecisionImpactPanel';
import { DecisionsTab } from './DecisionsTab';

const base: DecisionImpactResponse = {
  view: 'impact', subject_ref: 'spec:spec:decision:decision', authority: 'informational',
  decision_status: 'active', current_decision: true, data_source: 'composed',
  scope: { spec_ref: 'spec:spec', max_depth: 3, path_selection: 'one_representative_path_per_target_prefer_confirmed_link' },
  projection_freshness: { state: 'unknown', graph_generation: 'one', source_checkpoint: 'source',
    projection_checkpoint: null, checked_at: '2026-10-01T00:00:00Z' },
  completeness: { complete_for_scope: false, truncated: true, limitations: ['exploration_horizon_reached'] },
  counts: { observed_targets: 3, confirmed_link_targets: 2, potential_targets: 1 },
  frontier_refs: ['spec:spec:ac:other'], history: [{ subject_ref: 'spec:spec:decision:old', title: 'Old choice',
    status: 'superseded', supersedes_ref: null }], items: [], next_cursor: null,
};
function page(title: string, next: string | null = null): DecisionImpactResponse {
  return { ...base, next_cursor: next, items: [{ target_ref: title, title, target_type: 'TestScenario', status: 'ready',
    reach: 'indirect', certainty: 'potential', interpretation: 'potential_shared_card_reach',
    path: [{ source_ref: 'card:shared', relation: 'supports', target_ref: 'spec:spec:test_scenario:other',
      direction: 'outgoing', rule_id: 'supports/explicit@v1', layer: 'deterministic', created_by: 'worker_layer1',
      source_confirmed: true, graph_observed: true }] }] };
}
const props = { boardId: 'board', specId: 'spec', decisionId: 'decision', revision: '1', onOpenDecision: vi.fn() };
beforeEach(() => { vi.clearAllMocks(); api.getDecisionImpact.mockResolvedValue(base); });

it('labels KG57 shared-Card reach as potential even when its edge is observed', async () => {
  api.getDecisionImpact.mockResolvedValue(page('Other scenario'));
  render(<DecisionImpactPanel {...props} />);
  expect(await screen.findByText(/Other scenario · TestScenario · ready/)).toBeInTheDocument();
  expect(screen.getByText('Indirect · Potential reach')).toBeInTheDocument();
  expect(screen.getByText(/does not establish what the scenario tests/)).toBeInTheDocument();
  expect(screen.getByText(/Graph freshness: unknown/)).toBeInTheDocument();
  expect(screen.getByText(/Source link: confirmed; graph: observed/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /execute|start|repair/i })).not.toBeInTheDocument();
});

it('expands the bounded horizon on demand without claiming complete lineage', async () => {
  render(<DecisionImpactPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Explore further' }));
  await waitFor(() => expect(api.getDecisionImpact).toHaveBeenCalledTimes(2));
  expect(api.getDecisionImpact.mock.calls[1][3]).toEqual({ limit: 200, max_depth: 5, cursor: undefined });
  expect(screen.getByText(/Partial exploration/)).toBeInTheDocument();
});

it('paginates without adding global counts', async () => {
  api.getDecisionImpact.mockResolvedValueOnce(page('first', 'cursor')).mockResolvedValueOnce(page('second'));
  render(<DecisionImpactPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Load more impact' }));
  expect(await screen.findByText(/second · TestScenario/)).toBeInTheDocument();
  expect(screen.getByText(/first · TestScenario/)).toBeInTheDocument();
  expect(screen.getByText(/Targets reached: 3/)).toBeInTheDocument();
  expect(api.getDecisionImpact.mock.calls[1][3].cursor).toBe('cursor');
});

it.each([403, 409, 503, 504])('clears old observation on HTTP %s', async status => {
  api.getDecisionImpact.mockResolvedValueOnce(page('first', 'cursor')).mockRejectedValueOnce(new AuthenticatedFetchError({ status, message: 'failure' }));
  render(<DecisionImpactPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Load more impact' }));
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.queryByText(/first · TestScenario/)).not.toBeInTheDocument();
});

it('aborts Board changes and suppresses abandoned responses', async () => {
  let resolveOld!: (value: DecisionImpactResponse) => void;
  api.getDecisionImpact.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; })).mockResolvedValueOnce(page('new'));
  const view = render(<DecisionImpactPanel {...props} />);
  const signal = api.getDecisionImpact.mock.calls[0][4] as AbortSignal;
  view.rerender(<DecisionImpactPanel {...props} boardId="other" />);
  expect(signal.aborted).toBe(true);
  expect(await screen.findByText(/new · TestScenario/)).toBeInTheDocument();
  await act(async () => resolveOld(page('old')));
  expect(screen.queryByText(/old · TestScenario/)).not.toBeInTheDocument();
  view.unmount();
  expect((api.getDecisionImpact.mock.calls[1][4] as AbortSignal).aborted).toBe(true);
});

it('reloads source changes and keeps historical decisions distinct', async () => {
  api.getDecisionImpact.mockResolvedValue({ ...base, current_decision: false, decision_status: 'superseded' });
  const view = render(<DecisionImpactPanel {...props} />);
  expect(await screen.findByText(/historical Decision/)).toBeInTheDocument();
  fireEvent.click(screen.getByText('Old choice'));
  expect(props.onOpenDecision).toHaveBeenCalledWith('old');
  view.rerender(<DecisionImpactPanel {...props} revision="2" />);
  await waitFor(() => expect(api.getDecisionImpact).toHaveBeenCalledTimes(2));
  expect(api.getDecisionImpact.mock.calls[1][3].cursor).toBeUndefined();
});

it('loads from Decisions only on request and navigates supersedence without a write', async () => {
  const update = vi.fn();
  const spec = { id: 'spec', board_id: 'board', decisions: [
    { id: 'decision', title: 'Current choice', rationale: 'Reason', status: 'active' },
    { id: 'old', title: 'Old choice', rationale: 'Old reason', status: 'superseded' },
  ] } as Spec;
  render(<DecisionsTab spec={spec} onUpdate={update} />);
  expect(api.getDecisionImpact).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Current choice'));
  expect(api.getDecisionImpact).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Show impact' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Old choice' }));
  await waitFor(() => expect(api.getDecisionImpact).toHaveBeenCalledTimes(2));
  expect(api.getDecisionImpact.mock.calls[1][2]).toBe('old');
  expect(update).not.toHaveBeenCalled();
});
