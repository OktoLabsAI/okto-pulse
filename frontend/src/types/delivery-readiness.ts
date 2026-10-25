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

export interface PersistedReadinessSummary {
  card_id: string | null; card_type: 'normal' | 'bug' | 'test' | null; card_version: number | null;
  implementation: ReadinessMetric; verification: ReadinessMetric;
  population_known: boolean; evaluation_complete: boolean;
}
export interface PersistedReadinessProjection {
  contract_version: 'delivery-readiness-projection/v1';
  scope: { board_id: string; spec_id: string; spec_edition: number | null;
    spec_version: number; card_id: string | null };
  projection: { projection_version: 1; generation_id: string | null;
    source_revision: number | null; target_revision: number; evaluated_at: string | null;
    evaluator_version: string; freshness: 'current' | 'updating' | 'pending_initial' | 'unavailable';
    error_code: string | null };
  summaries: PersistedReadinessSummary[];
  units: Array<{card_id: string | null; kind: 'obligation' | 'scenario'; ref: string;
    semantic_sha256: string; spec_edition: number; contribution_card_ids: string[];
    implementation_state: string; verification_state: string;
    implementation_reason_codes: string[]; verification_reason_codes: string[] }>;
  page: { offset: number; limit: number; total: number; has_more: boolean; next_cursor: string | null };
  mutation_reauthorization_required: true;
}

export function isPersistedReadinessProjection(value: unknown): value is PersistedReadinessProjection {
  return object(value) && value.contract_version === 'delivery-readiness-projection/v1'
    && object(value.scope) && typeof value.scope.board_id === 'string' && typeof value.scope.spec_id === 'string'
    && object(value.projection) && value.projection.projection_version === 1
    && ['current', 'updating', 'pending_initial', 'unavailable'].includes(String(value.projection.freshness))
    && (!['current', 'updating'].includes(String(value.projection.freshness))
      || typeof value.projection.generation_id === 'string' && value.projection.generation_id.length > 0)
    && Array.isArray(value.summaries) && value.summaries.every(summary => object(summary)
      && (summary.card_id === null || typeof summary.card_id === 'string')
      && metric(summary.implementation) && metric(summary.verification));
}

export interface ReadinessQuery {
  card_id?: string; phase?: 'all' | 'implementation' | 'verification' | 'lifecycle';
  snapshot_id?: string; issue_id?: string; limit?: number; cursor?: string;
}
export interface ReadinessPage {
  total: number; returned: number; has_more: boolean; next_cursor: string | null;
  response_truncated: boolean; phase_filter: string;
}
export interface ReadinessAction {
  id: string; kind: string; message_code: string; message_params: Record<string, unknown>; message: string;
  issue_ids: string[]; state: 'ready' | 'waiting_dependency' | 'requires_input' | 'requires_human' | 'forbidden' | 'unavailable';
  depends_on_action_ids: string[]; responsible_card_ids: string[]; permission: string | null;
  execution_surface: 'pulse_tool' | 'external_agent' | 'human_decision' | 'read_only'; pulse_tool: string | null;
  arguments: Record<string, unknown>; missing_inputs: string[]; context_refs: string[]; resolves_unit_count: number;
}
export interface ReadinessUnit { unit: 'obligation' | 'scenario'; ref: string; semantic_sha256: string; spec_edition: number; card_id: string | null }
export interface ReadinessEvidence { kind: string; id: string; source_ref: string | null; revision: string | null }
export interface ReadinessFact {
  source_ref?: string | null; revision?: string | null; record_id?: string | null; receipt_id?: string | null;
  scenario_id?: string | null; criterion_id?: string | null; verification_method?: string | null; outcome?: string | null;
  scope_sha256?: string | null; native_reason_codes?: string[]; obligation_ref?: string | null; semantic_sha256?: string | null;
  source_obligation_ref?: string | null; source_scope_sha256?: string | null; execution_id?: string | null;
  target_id?: string | null; observation_id?: string | null; card_id?: string | null; text?: string | null;
}
export interface ReadinessPinpoint {
  kind: string; entity_type: string; entity_id: string; source_ref: string | null; revision: string | null;
  relative_path: string | null; qualified_symbol: string | null; line_start: number | null; line_end: number | null;
  target_id: string | null; execution_id: string | null; record_id: string | null; observation_id: string | null;
  precision: 'entity_only' | 'target' | 'symbol' | 'exact_span'; origin: string;
}
export interface ReadinessIssue {
  detail_state: 'available' | 'restricted'; id: string; code: string; phase: string; score_impact: string;
  intrinsic_state: string; blocks_proof: boolean; blocks_target_transition: boolean | null;
  message_code: string; message_params: Record<string, unknown>; message: string; native_reason_codes: string[];
  affected_unit_count: number; affected_units: ReadinessUnit[]; affected_units_truncated: boolean;
  criterion_ids: string[]; criterion_count: number; criterion_ids_truncated: boolean;
  scenario_ids: string[]; scenario_count: number; scenario_ids_truncated: boolean;
  implementation_card_ids: string[]; implementation_card_count: number; implementation_card_ids_truncated: boolean;
  test_card_ids: string[]; test_card_count: number; test_card_ids_truncated: boolean;
  owner: string | null; owner_state: 'known' | 'unassigned' | 'restricted';
  expected: ReadinessFact[]; expected_count: number; expected_truncated: boolean;
  observed: ReadinessFact[]; observed_count: number; observed_truncated: boolean;
  evidence_refs: ReadinessEvidence[]; evidence_count: number; evidence_refs_truncated: boolean;
  pinpoints: ReadinessPinpoint[]; pinpoint_count: number; pinpoints_truncated: boolean;
  root_cause_id: string; depends_on_issue_ids: string[]; action_ids: string[];
  detail_query: Omit<ReadinessQuery, 'card_id'> & { board_id: string; spec_id: string; card_id: string | null; view: 'readiness'; issue_id: string; snapshot_id: string };
}
export interface ReadinessResponse extends DeliveryReadiness {
  issues: ReadinessIssue[]; next_actions: ReadinessAction[]; page: ReadinessPage;
}
export type ReadinessEntry =
  | { id: string; type: 'affected_unit'; section: 'affected_units'; value: ReadinessUnit }
  | { id: string; type: 'evidence'; section: 'evidence_refs'; value: ReadinessEvidence }
  | { id: string; type: 'evidence'; section: 'expected' | 'observed'; value: ReadinessFact }
  | { id: string; type: 'evidence'; section: 'relations'; value: { kind: string; source_id: string; target_id: string | null } }
  | { id: string; type: 'pinpoint'; section: 'pinpoints'; value: ReadinessPinpoint }
  | { id: string; type: 'action'; section: 'actions'; value: ReadinessAction };
export interface ReadinessDetail extends DeliveryReadiness {
  detail_state: 'available' | 'restricted'; issue_id: string;
  entry_counts: { affected_unit: number; evidence: number; pinpoint: number; action: number };
  entries: ReadinessEntry[]; page: ReadinessPage;
}
function page(value: unknown): value is ReadinessPage {
  return object(value) && Number.isInteger(value.total) && Number(value.total) >= 0
    && Number.isInteger(value.returned) && Number(value.returned) >= 0 && Number(value.returned) <= 20
    && typeof value.has_more === 'boolean' && typeof value.response_truncated === 'boolean'
    && (value.has_more ? typeof value.next_cursor === 'string' && value.next_cursor.length > 0 : value.next_cursor === null);
}
export function isReadinessResponse(value: unknown): value is ReadinessResponse {
  if (!isDeliveryReadiness(value) || !object(value)) return false;
  return page(value.page) && Array.isArray(value.issues) && value.issues.length === value.page.returned
    && Array.isArray(value.next_actions) && value.next_actions.every(action)
    && value.issues.every(issue => object(issue) && typeof issue.id === 'string' && typeof issue.message === 'string'
      && typeof issue.phase === 'string' && typeof issue.intrinsic_state === 'string'
      && typeof issue.owner_state === 'string'
      && ['affected_units', 'criterion_ids', 'scenario_ids', 'implementation_card_ids', 'test_card_ids',
        'expected', 'observed', 'evidence_refs', 'pinpoints', 'depends_on_issue_ids', 'action_ids'].every(key => Array.isArray(issue[key]))
      && object(issue.detail_query) && issue.detail_query.snapshot_id === value.snapshot_id
      && issue.detail_query.board_id === value.scope.board_id && issue.detail_query.spec_id === value.scope.spec_id
      && issue.detail_query.card_id === value.scope.card_id);
}
function action(value: unknown): value is ReadinessAction {
  return object(value) && typeof value.id === 'string' && typeof value.message === 'string'
    && ['ready', 'waiting_dependency', 'requires_input', 'requires_human', 'forbidden', 'unavailable'].includes(String(value.state))
    && typeof value.execution_surface === 'string' && object(value.arguments)
    && ['issue_ids', 'depends_on_action_ids', 'responsible_card_ids', 'missing_inputs', 'context_refs'].every(key => Array.isArray(value[key]));
}
export function isReadinessDetail(value: unknown): value is ReadinessDetail {
  return isDeliveryReadiness(value) && object(value) && typeof value.issue_id === 'string'
    && page(value.page) && Array.isArray(value.entries) && value.entries.length === value.page.returned
    && value.entries.every(entry => object(entry) && typeof entry.id === 'string' && object(entry.value)
      && ['affected_units', 'evidence_refs', 'expected', 'observed', 'relations', 'pinpoints', 'actions'].includes(String(entry.section))
      && (entry.section !== 'actions' || action(entry.value)));
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
