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
import { cleanup, render, screen, waitFor } from '@testing-library/react';

import { DecisionsTab } from '../DecisionsTab';
import * as kgHealthApi from '@/services/kg-health-api';
import type { Spec } from '@/types';

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
    ideation_id: null,
    refinement_id: null,
    title: 'Test spec',
    description: null,
    context: null,
    functional_requirements: ['FR'],
    technical_requirements: null,
    acceptance_criteria: ['AC'],
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
});

afterEach(() => {
  cleanup();
});

describe('DecisionsTab cognitive badge wiring', () => {
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
});
