import type { DeliveryReadiness, ReadinessCounts, ReadinessMetric } from '@/types/delivery-readiness';
import type { CardSummary } from '@/types';

export const emptyCounts: ReadinessCounts = { satisfied: 0, missing: 0, partial: 0, failed: 0,
  stale: 0, conflict: 0, waived: 0, not_applicable: 0, unknown: 0 };
export const metric = (percent: number, satisfied: number): ReadinessMetric => ({
  unit: 'obligation', status: 'known', percent, total: 8, applicable: 8, satisfied,
  not_applicable: 0, waived: 0, unresolved: 8 - satisfied, unknown: 0,
  counts_by_state: { ...emptyCounts, satisfied, missing: 8 - satisfied }, reason_codes: [], known_counts: null,
});
export function readiness(cardId: string | null = 'c'): DeliveryReadiness {
  return { contract_version: 'delivery-readiness/v1', metrics_version: 2,
    scope: { board_id: 'b', spec_id: 's', card_id: cardId, card_type: cardId ? 'normal' : null,
      spec_edition: 1, spec_version: 1, card_version: cardId ? 1 : null },
    snapshot_id: 'a'.repeat(64), evaluated_at: '2026-10-14T00:00:00Z',
    scope_complete: true, evaluation_complete: true,
    implementation: metric(100, 8), verification: metric(75, 6),
    delivery_state: 'verification_pending', proof_complete: false,
    delivery_gate: { status: 'blocked', mode: 'blocking', exception_applied: false, exception_refs: [] },
    lifecycle: { stored_status: 'in_progress', target: 'done', check_status: 'not_evaluated',
      other_blocker_count: null, qualification_is_current: null, reason_codes: [], context_refs: [] },
    summary: { message_code: 'verification_pending', message: 'Two obligations need verification.',
      unresolved_units: 2, root_causes: 2, lifecycle_pending: null, exceptions: 0 },
    mutation_reauthorization_required: true };
}
export const task: CardSummary = { id: 'c', board_id: 'b', spec_id: 's', card_type: 'normal',
  title: 'Implement capacity', status: 'in_progress', labels: [], created_at: '2026-10-09',
  updated_at: '2026-10-09', description: null, priority: 'none', position: 0, assignee_id: null,
  created_by: 'agent', due_date: null, test_scenario_ids: null, conclusions: [], validations: [] };
export const cards: CardSummary[] = [task, { ...task, id: 'other' }];
export const boardSummary = () => ({ contract_version: 'delivery-readiness/v1', metrics_version: 2,
  delivery_readiness: readiness(null), cards: cards.map(card => ({ card_id: card.id, delivery_readiness: readiness(card.id) })) });
