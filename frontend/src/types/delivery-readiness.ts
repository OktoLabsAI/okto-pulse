/** Current factual counts; never derived from the deprecated maturity score. */
export interface ReadinessCounts {
  satisfied: number; missing: number; partial: number; failed: number; stale: number;
  conflict: number; waived: number; not_applicable: number; unknown: number;
}
export interface ReadinessMetric {
  unit: 'obligation' | 'scenario'; status: 'known' | 'unknown' | 'not_applicable';
  percent: number | null; total: number | null; applicable: number | null;
  satisfied: number; not_applicable: number; waived: number; unresolved: number | null; unknown: number;
  counts_by_state: ReadinessCounts; reason_codes: string[]; known_counts: ReadinessCounts | null;
}
export type DeliveryState = 'unknown' | 'implementation_pending' | 'verification_pending' | 'verification_failed'
  | 'ready' | 'delivered' | 'ready_with_exceptions' | 'delivered_with_exceptions' | 'not_applicable';
export interface DeliveryReadiness {
  contract_version: 'delivery-readiness/v1'; metrics_version: 2;
  scope: { board_id: string; spec_id: string; card_id: string | null; card_type: 'normal' | 'bug' | 'test' | null;
    spec_edition: number; spec_version: number; card_version: number | null };
  snapshot_id: string; evaluated_at: string; scope_complete: boolean; evaluation_complete: boolean;
  implementation: ReadinessMetric; verification: ReadinessMetric;
  delivery_state: DeliveryState; proof_complete: boolean | null;
  delivery_gate: { status: 'allowed' | 'blocked' | 'unknown'; mode: 'blocking' | 'advisory'; exception_applied: boolean; exception_refs: string[] };
  lifecycle: { stored_status: string; target: 'done'; check_status: 'allowed' | 'blocked' | 'not_evaluated';
    other_blocker_count: number | null; qualification_is_current: boolean | null; reason_codes: string[]; context_refs: string[] };
  summary: { message_code: string; message: string; unresolved_units: number | null; root_causes: number;
    lifecycle_pending: number | null; exceptions: number };
  mutation_reauthorization_required: true;
}
export interface DeliveryReadinessBoardSummary {
  contract_version: 'delivery-readiness/v1'; metrics_version: 2;
  delivery_readiness: DeliveryReadiness;
  cards: { card_id: string; delivery_readiness: DeliveryReadiness }[];
}

function object(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null; }
function metric(value: unknown): value is ReadinessMetric {
  if (!object(value) || !object(value.counts_by_state)) return false;
  const count = (n: unknown) => Number.isInteger(n) && Number(n) >= 0;
  const nullableCount = (n: unknown) => n === null || count(n);
  return ['obligation', 'scenario'].includes(String(value.unit))
    && ['known', 'unknown', 'not_applicable'].includes(String(value.status))
    && (value.status === 'known' ? count(value.percent) && Number(value.percent) <= 100 : value.percent === null)
    && ['total', 'applicable', 'unresolved'].every(key => nullableCount(value[key]))
    && ['satisfied', 'not_applicable', 'waived', 'unknown'].every(key => count(value[key]))
    && ['satisfied', 'missing', 'partial', 'failed', 'stale', 'conflict', 'waived', 'not_applicable', 'unknown'].every(key => count((value.counts_by_state as Record<string, unknown>)[key]));
}
/** Validate the transport shape/version only. Sufficiency and formulas belong to Core. */
export function isDeliveryReadiness(value: unknown): value is DeliveryReadiness {
  return object(value) && value.contract_version === 'delivery-readiness/v1' && value.metrics_version === 2
    && object(value.scope) && typeof value.scope.board_id === 'string' && typeof value.scope.spec_id === 'string'
    && (value.scope.card_id === null || typeof value.scope.card_id === 'string')
    && typeof value.snapshot_id === 'string' && /^[a-f0-9]{64}$/.test(value.snapshot_id)
    && metric(value.implementation) && metric(value.verification)
    && object(value.summary) && typeof value.summary.message_code === 'string'
    && object(value.lifecycle) && typeof value.lifecycle.stored_status === 'string'
    && object(value.delivery_gate) && typeof value.delivery_gate.status === 'string'
    && ['unknown', 'implementation_pending', 'verification_pending', 'verification_failed', 'ready', 'delivered',
      'ready_with_exceptions', 'delivered_with_exceptions', 'not_applicable'].includes(String(value.delivery_state));
}
