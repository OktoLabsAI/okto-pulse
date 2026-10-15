/**
 * DecisionsTab cognitive badge wiring — KG-03A.6.
 *
 * Validates that the spec decisions tab uses the batch
 * useCognitivePendingBadges hook with the canonical
 * `decision:<spec_id>:<decision_id>` source_refs (the shape emitted by
 * the core ``BoardSourceStore`` and matched by
 * ``cognitive_badge_resolver``). The previous wiring keyed badges by
 * `decision:<id>` which silently produced not_found for every real
 * rebuild-generated pending decision item (KG-03A.6 val_ff050455).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import { DecisionsTab } from '../DecisionsTab';
import * as kgHealthApi from '@/services/kg-health-api';
import type { Spec } from '@/types';
import type { DecisionReviewProjection } from '@/types/decision-reviews';
import { AuthenticatedFetchError } from '@/lib/authFetch';

vi.mock('@/services/api', () => ({ useDashboardApi: () => decisionApi }));
const decisionApi = { getDecisionReviews: vi.fn(), recordDecisionReviews: vi.fn() };

const { hasPermission } = vi.hoisted(() => ({
  hasPermission: vi.fn(),
}));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: hasPermission,
  }),
}));

vi.mock('@/services/kg-health-api', async () => {
  const actual = await vi.importActual<typeof kgHealthApi>(
    '@/services/kg-health-api',
  );
  return {
    ...actual,
    getKGCognitivePendingBadges: vi.fn(),
  };
});

const BOARD_ID = 'board-decisions-tab';

function buildSpec(): Spec {
  return {
    id: 'spec_test',
    board_id: BOARD_ID,
    version: 3,
    edition: 1,
    ideation_id: null,
    refinement_id: null,
    title: 'Test spec',
    description: null,
    context: null,
    functional_requirements: [{ id: 'fr', title: 'Behavior', text: 'FR' }],
    technical_requirements: null,
    acceptance_criteria: [{ id: 'ac', title: 'Criterion', text: 'AC' }],
    test_scenarios: null,
    business_rules: null,
    api_contracts: null,
    integration_requirements: null,
    observability_requirements: null,
    decisions: [
      {
        id: 'dec_alpha',
        title: 'Alpha decision',
        rationale: 'reason',
        context: null,
        alternatives_considered: null,
        supersedes_decision_id: null,
        linked_requirements: null,
        linked_task_ids: null,
        status: 'active',
        notes: null,
      },
      {
        id: 'dec_beta',
        title: 'Beta decision',
        rationale: 'reason b',
        context: null,
        alternatives_considered: null,
        supersedes_decision_id: null,
        linked_requirements: null,
        linked_task_ids: null,
        status: 'active',
        notes: null,
      },
    ],
    screen_mockups: null,
    skip_test_coverage: true,
    status: 'approved',
  } as unknown as Spec;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(kgHealthApi.getKGCognitivePendingBadges).mockReset().mockRejectedValue(new Error('Graph unavailable in this isolated test'));
  decisionApi.getDecisionReviews.mockReset().mockResolvedValue({ ...review(), decisions: [], complete: false });
  decisionApi.recordDecisionReviews.mockReset().mockResolvedValue({ id: 'record', replayed: false });
  hasPermission.mockReturnValue(true);
});

function review(version = 3): DecisionReviewProjection {
  return { board_id: BOARD_ID, spec_id: 'spec_test', edition: 1, version, review_revision: String(version).repeat(64),
    complete: true, observation_authority: 'authenticated_reviewer_declaration', history: [],
    decisions: [{ decision_id: 'dec_alpha', status: 'verified', record_ids: [],
      basis: { scope_sha256: String(version).repeat(64), expected: `Inspection condition ${version}`,
        sources: [{ reference: 'spec:spec_test', revision: String(version), sha256: 'a'.repeat(64) }] },
      separation: { mode: 'warn', allowed: true, warning: false, conflicts: [] } }] };
}

describe('Decision inspection refresh', () => {
  it('keeps a draft across editions and blocks stale or mismatched review responses', async () => {
    decisionApi.getDecisionReviews.mockResolvedValueOnce(review()).mockResolvedValueOnce(review(4));
    const spec = buildSpec();
    const { rerender } = render(<DecisionsTab spec={spec} canReview onUpdate={vi.fn()} />);
    fireEvent.click(screen.getByText('Alpha decision'));
    fireEvent.click(await screen.findByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Edition one observation' } });
    rerender(<DecisionsTab spec={{ ...spec, version: 4, edition: 2 }} canReview onUpdate={vi.fn()} />);
    await screen.findByText(/Decision verification could not be loaded/);
    expect(screen.getByLabelText('Observed result')).toHaveValue('Edition one observation');
    expect(screen.getByRole('button', { name: 'Record observation' })).toBeDisabled();
    decisionApi.getDecisionReviews.mockResolvedValueOnce({ ...review(4), edition: 2 });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh verification' }));
    await screen.findByRole('button', { name: 'Review inspection base' });
    expect(screen.getByLabelText('Observed result')).toHaveValue('Edition one observation');
    expect(screen.getByRole('button', { name: 'Record observation' })).toBeDisabled();
  });

  it('keeps the original observation when only the review head changes', async () => {
    decisionApi.getDecisionReviews.mockResolvedValueOnce(review()).mockResolvedValueOnce({ ...review(), review_revision: 'e'.repeat(64) });
    render(<DecisionsTab spec={buildSpec()} canReview onUpdate={vi.fn()} />);
    fireEvent.click(screen.getByText('Alpha decision'));
    fireEvent.click(await screen.findByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Do not reconcile an unseen observation' } });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh verification' }));
    await screen.findByRole('button', { name: 'Review inspection base' });
    expect(screen.getByLabelText('Observed result')).toHaveValue('Do not reconcile an unseen observation');
    expect(screen.getByRole('button', { name: 'Record observation' })).toBeDisabled();
  });

  it('keeps the observation through a pending read without presenting stale verification as current', async () => {
    let complete!: (value: DecisionReviewProjection) => void;
    decisionApi.getDecisionReviews.mockResolvedValueOnce(review()).mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
    const spec = buildSpec();
    const { rerender } = render(<DecisionsTab spec={spec} canReview onUpdate={vi.fn()} />);
    fireEvent.click(screen.getByText('Alpha decision'));
    fireEvent.click(await screen.findByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Preserve this observed condition' } });
    rerender(<DecisionsTab spec={{ ...spec, version: 4 }} canReview onUpdate={vi.fn()} />);
    expect(screen.getByLabelText('Observed result')).toHaveValue('Preserve this observed condition');
    expect(screen.getByRole('button', { name: 'Record observation' })).toBeDisabled();
    expect(screen.queryByRole('progressbar', { name: 'Decision adherence' })).not.toBeInTheDocument();
    await act(async () => complete(review(4)));
    expect(screen.getByLabelText('Observed result')).toHaveValue('Preserve this observed condition');
    expect(screen.getByRole('button', { name: 'Record observation' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Review inspection base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Record observation' }));
    await waitFor(() => expect(decisionApi.recordDecisionReviews).toHaveBeenCalledOnce());
    expect(decisionApi.recordDecisionReviews.mock.calls[0][2]).toMatchObject({ expected_version: 4, entries: [{ observed: 'Preserve this observed condition' }] });
  });

  it.each(['removed', 'network', 'denied'])('handles %s without losing authorized draft or exposing denied data', async state => {
    decisionApi.getDecisionReviews.mockResolvedValueOnce(review());
    const spec = buildSpec();
    const { rerender } = render(<DecisionsTab spec={spec} canReview onUpdate={vi.fn()} />);
    fireEvent.click(screen.getByText('Alpha decision'));
    fireEvent.click(await screen.findByText('Record inspection'));
    fireEvent.change(screen.getByLabelText('Observed result'), { target: { value: 'Local observation to preserve' } });
    if (state === 'removed') decisionApi.getDecisionReviews.mockResolvedValueOnce({ ...review(4), decisions: [] });
    else decisionApi.getDecisionReviews.mockRejectedValueOnce(new AuthenticatedFetchError({ status: state === 'denied' ? 403 : 500, message: 'provider detail' }));
    rerender(<DecisionsTab spec={{ ...spec, version: 4, ...(state === 'removed' ? { decisions: [] } : {}) }} canReview onUpdate={vi.fn()} />);
    await waitFor(() => expect(decisionApi.getDecisionReviews).toHaveBeenCalledTimes(2));
    if (state === 'denied') {
      await waitFor(() => expect(screen.queryByDisplayValue('Local observation to preserve')).not.toBeInTheDocument());
    } else {
      expect(await screen.findByDisplayValue('Local observation to preserve')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Record observation' })).toBeDisabled();
    }
    expect(decisionApi.recordDecisionReviews).not.toHaveBeenCalled();
  });
});

afterEach(() => {
  cleanup();
});

describe('DecisionsTab cognitive badge wiring', () => {
  it('never renders a complete progress bar from an incomplete evidence population', async () => {
    vi.mocked(kgHealthApi.getKGCognitivePendingBadges).mockRejectedValueOnce(new Error('Graph unavailable'));
    decisionApi.getDecisionReviews.mockResolvedValueOnce({ ...review(), complete: false, history: [],
      decisions: [{ decision_id: 'dec_alpha', status: 'verified' }, { decision_id: 'dec_beta', status: 'verified' }] });
    render(<DecisionsTab spec={buildSpec()} onUpdate={() => undefined} />);
    await screen.findAllByText('Verified');
    expect(screen.getByText('Incomplete / unavailable')).toBeInTheDocument();
    expect(screen.queryByRole('progressbar', { name: 'Decision adherence' })).not.toBeInTheDocument();
    expect(screen.queryByText('100% verified')).not.toBeInTheDocument();
  });

  it('requests one batched GET with the canonical `decision:<spec_id>:<decision_id>` source_refs', async () => {
    vi.mocked(kgHealthApi.getKGCognitivePendingBadges).mockResolvedValue({
      board_id: BOARD_ID,
      readonly: true,
      selected_kg_generation_id: null,
      eligible_entity_types: [
        'spec',
        'decision',
        'refinement',
        'task',
        'test',
        'bug',
      ],
      badges: {
        'decision:spec_test:dec_alpha': {
          show_badge: true,
          label: 'Pending cognitive consolidation',
          status: 'pending',
          item_id: 'item_1',
          updated_at: null,
          reason: 'active_cognitive_item',
        },
        'decision:spec_test:dec_beta': {
          show_badge: false,
          label: '',
          status: null,
          item_id: null,
          updated_at: null,
          reason: 'not_found',
        },
      },
    });
    render(
      <DecisionsTab spec={buildSpec()} onUpdate={() => undefined} />,
    );
    await waitFor(() => {
      expect(
        kgHealthApi.getKGCognitivePendingBadges,
      ).toHaveBeenCalledTimes(1);
    });
    expect(kgHealthApi.getKGCognitivePendingBadges).toHaveBeenCalledWith(
      BOARD_ID,
      expect.arrayContaining([
        'decision:spec_test:dec_alpha',
        'decision:spec_test:dec_beta',
      ]),
      expect.any(Object),
      expect.any(AbortSignal),
    );
    const refsSent = vi.mocked(kgHealthApi.getKGCognitivePendingBadges).mock
      .calls[0]?.[1] as string[];
    // Negative assertion: the legacy `decision:<id>` key must NOT leak
    // into the request — the resolver would silently return not_found.
    expect(refsSent).not.toContain('decision:dec_alpha');
    expect(refsSent).not.toContain('decision:dec_beta');

    const badges = await screen.findAllByTestId('cognitive-pending-badge');
    // Only dec_alpha has show_badge=true; dec_beta hides itself.
    expect(badges).toHaveLength(1);
    expect(badges[0].getAttribute('data-status')).toBe('pending');
  });

  it('does not request cognitive badges without kg.operations.cognitive.read', async () => {
    hasPermission.mockReturnValue(false);

    render(
      <DecisionsTab spec={buildSpec()} onUpdate={() => undefined} />,
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(kgHealthApi.getKGCognitivePendingBadges).not.toHaveBeenCalled();
  });
});
