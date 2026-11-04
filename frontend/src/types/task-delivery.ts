export type TaskDeliveryState = 'updating_or_unknown' | 'optional_or_waived' | 'verification_failed'
  | 'historical_done' | 'implementation_pending' | 'verification_pending' | 'completion_pending' | 'delivered' | 'ready';
export interface TaskDeliverySummary {
  contract_version: 'task-delivery-summary/v1';
  board_id: string; spec_id: string; card_id: string; spec_edition: number; card_version: number;
  snapshot_id: string | null; population_known: boolean; evaluation_complete: boolean;
  state: TaskDeliveryState; reason_code: string; reason: string; historical_status: string;
  pending_count: number | null;
  next_action: { kind: 'record_progress' | 'submit_report' | 'conclude' | 'open_details' | 'retry';
    label: string; owner: 'user' | 'technical_owner' | 'system'; mutation_reauthorization_required: true } | null;
  last_update: { record_id: string; content: string; remaining: string | null; submitted_at: string;
    origin: 'human_submission' | 'technical_checkpoint'; text_truncated: boolean } | null;
}
export interface HumanTaskSubmission {
  contract_version: 'human-task-submission/v1'; action: 'progress' | 'delivery_report';
  content: string; remaining?: string; expected_card_version: number; expected_spec_edition: number;
  idempotency_key: string; requested_status?: 'validation' | 'done'; supersedes_submission_id?: string;
}
export interface HumanTaskSubmissionResult {
  replayed: boolean; submission_id: string; receipt_id: string; status: string;
  card_version: number; spec_edition: number; summary: TaskDeliverySummary;
}
const states: TaskDeliveryState[] = ['updating_or_unknown', 'optional_or_waived', 'verification_failed',
  'historical_done', 'implementation_pending', 'verification_pending', 'completion_pending', 'delivered', 'ready'];
/** Transport validation only; the verdict and next action are owned by Core. */
export function isTaskDeliverySummary(value: unknown): value is TaskDeliverySummary {
  if (!value || typeof value !== 'object') return false;
  const v = value as TaskDeliverySummary;
  return v.contract_version === 'task-delivery-summary/v1' && states.includes(v.state)
    && typeof v.reason === 'string' && typeof v.historical_status === 'string'
    && typeof v.board_id === 'string' && typeof v.spec_id === 'string' && typeof v.card_id === 'string'
    && Number.isInteger(v.spec_edition) && v.spec_edition > 0 && Number.isInteger(v.card_version) && v.card_version > 0
    && typeof v.population_known === 'boolean' && typeof v.evaluation_complete === 'boolean'
    && (v.next_action === null || !!v.next_action && ['record_progress', 'submit_report', 'conclude', 'open_details', 'retry'].includes(v.next_action.kind));
}
