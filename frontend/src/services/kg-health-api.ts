/**
 * API client for the KG health snapshot endpoint (spec d754d004).
 *
 * Polled by KGHealthView every 30s while the overlay is mounted and the
 * tab is visible. Backend mirror: core/api/kg_health.py (KGHealthResponse).
 *
 * Mirror do padrão de queue-health-api.ts — interface tipada + função
 * fetch com AbortSignal. Sem libs novas; usa fetch nativo + cookie do
 * Clerk injetado pelo authAdapter.
 */

export interface TopDisconnectedNode {
  id: string;
  type: string;
  degree: number;
}

export interface KGHealthIssue {
  code: string;
  component: string;
  severity: string;
  reason: string;
  description: string;
  operator_action: string;
}

export interface DecaySchedulerDiagnostics {
  status: 'ok' | 'never_run' | 'stale' | 'failed' | 'running' | 'unknown' | string;
  severity: 'info' | 'warning' | 'critical' | string;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  next_scheduled_at: string | null;
  stale_tolerance_seconds: number | null;
  recommended_action: string;
  operational_debt: boolean;
  graph_recovery_required: boolean;
  reason: string | null;
  running_started_at?: string | null;
  source?: string;
}

export interface StorageFootprintProxy {
  source: 'file_size_proxy' | string;
  status: string;
  percentage: number | null;
  high_water_mark_pct: number | null;
  graph_lbug_bytes: number | null;
  sidecar_bytes: number | null;
  total_bytes: number | null;
  configured_max_db_size_bytes: number | null;
  configured_max_db_size_gb: number | null;
  is_direct_memory_telemetry: boolean;
  description: string;
  tooltip: string;
  unavailable_reason: string | null;
}

export interface KGHealth {
  queue_depth: number;
  oldest_pending_age_s: number;
  dead_letter_count: number;
  total_nodes: number;
  default_score_count: number;
  default_score_ratio: number;
  avg_relevance: number;
  top_disconnected_nodes: TopDisconnectedNode[];
  schema_version: string;
  health_schema_version?: string;
  graph_schema_version?: string | null;
  contradict_warn_count: number;
  last_decay_tick_at: string | null;
  last_tick_status?: 'running' | 'completed' | 'failed' | string | null;
  last_tick_error?: string | null;
  nodes_recomputed_in_last_tick: number;
  // True quando o advisory lock global ``kg_daily_tick`` está acquired —
  // serve para desabilitar o botão "Run tick now" mesmo após remount do
  // componente (single source of truth via backend).
  tick_in_progress?: boolean;
  // KG-01 health state classifier (sm_a30278ad mockup → Recovery panel).
  graph_state?: string;
  discovery_state?: string;
  overall_state?: string;
  metric_status?: 'available' | 'unavailable' | string;
  current_kg_generation_id?: string | null;
  classification_reason?: string | null;
  health_issues?: KGHealthIssue[];
  decay_scheduler_diagnostics?: DecaySchedulerDiagnostics;
  storage_footprint_proxy?: StorageFootprintProxy;
}

// ---- KG-02 rebuild lifecycle (spec e7360ffe, mockup sm_a30278ad) -------

export interface RebuildPreflightResult {
  board_id: string;
  outcome: 'ready' | 'confirmation_required' | 'blocked' | string;
  action_required: string;
  reason: string | null;
  base_state: string;
  metric_status: string;
  current_kg_generation_id: string | null;
  eligible_source_count: number;
  skipped_cancelled_count: number;
  has_non_deterministic_inputs: boolean;
  preflight_hash: string;
  generated_at: string;
  rebuild_status?: string;
  operational_substatus?: string;
  manifest_ref: string;
  source_set_hash: string;
}

export interface RebuildConfirmResult {
  confirmation_id: string;
  manifest_ref: string;
  source_set_hash: string;
  expires_at: string;
}

export interface RebuildRunResult {
  run_id: string;
  outcome: 'completed' | 'failed' | 'rebuild_failed' | 'report_persist_failed'
    | 'confirmation_required' | 'manifest_drift' | 'lock_contention'
    | 'unsupported_operation' | string;
  reason: string;
  audit_ref: string;
  previous_kg_generation_id: string | null;
  current_kg_generation_id: string | null;
  started_at: string;
  finished_at: string;
  affected_files: string[];
  report_ref?: string | null;
  report_id?: string | null;
  publishable_status?: string | null;
  promotion_outcome?: string | null;
  operator_action?: string | null;
  event_emitted?: boolean;
}

async function postJSON<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const msg =
      typeof err.detail === 'string'
        ? err.detail
        : err.detail?.reason || err.detail?.error || err.message || `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return resp.json();
}

export async function runRebuildPreflight(
  boardId: string,
  signal?: AbortSignal,
): Promise<RebuildPreflightResult> {
  const resp = await fetch(
    `${BASE}/kg/rebuild/preflight?board_id=${encodeURIComponent(boardId)}`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal },
  );
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
  }
  return resp.json();
}

export interface RebuildConfirmRequest {
  board_id: string;
  operation: string;
  preflight_hash: string;
  manifest_ref: string;
}

export function runRebuildConfirm(
  body: RebuildConfirmRequest,
  signal?: AbortSignal,
): Promise<RebuildConfirmResult> {
  return postJSON<RebuildConfirmResult>('/kg/rebuild/confirm', body, signal);
}

export interface RebuildRunRequest extends RebuildConfirmRequest {
  confirmation_id: string;
  reason: string;
}

export function runRebuildRun(
  body: RebuildRunRequest,
  signal?: AbortSignal,
): Promise<RebuildRunResult> {
  return postJSON<RebuildRunResult>('/kg/rebuild/run', body, signal);
}

const BASE = '/api/v1';

export async function getKGHealth(
  boardId: string,
  signal?: AbortSignal,
): Promise<KGHealth> {
  const resp = await fetch(
    `${BASE}/kg/health?board_id=${encodeURIComponent(boardId)}`,
    {
      headers: { 'Content-Type': 'application/json' },
      signal,
    },
  );
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
  }
  return resp.json();
}

// ---- KG-03.4/3.5 cognitive pending (api_cce40fa6 + api_897dde99) -------

export type KGCognitiveItemStatus =
  | 'pending'
  | 'in_progress'
  | 'consolidated'
  | 'skipped'
  | 'failed';

export interface KGCognitivePendingItem {
  item_id: string;
  source_ref: string;
  artifact_type: string;
  status: KGCognitiveItemStatus | string;
  recorded_at: string;
  updated_at: string | null;
  updated_by_agent_id: string | null;
  consolidation_session_id: string | null;
  reason_code: string | null;
}

export interface KGCognitivePendingCounts {
  pending: number;
  in_progress: number;
  consolidated: number;
  skipped: number;
  failed: number;
  total: number;
}

export interface KGCognitivePendingResponse {
  board_id: string;
  selected_kg_generation_id: string | null;
  readonly: true;
  legacy_mode: boolean;
  counts: KGCognitivePendingCounts;
  items: KGCognitivePendingItem[];
}

export interface GetKGCognitivePendingOptions {
  kgGenerationId?: string | null;
  status?: KGCognitiveItemStatus;
  limit?: number;
  offset?: number;
}

/**
 * Read-only fetch for the cognitive pending feedback panel
 * (api_cce40fa6). Mirrors the REST endpoint contract: never mutates,
 * never echoes raw artifact bodies. Errors propagate so the panel can
 * show a non-blocking error state without breaking KGHealthView.
 */
export async function getKGCognitivePendingItems(
  boardId: string,
  options: GetKGCognitivePendingOptions = {},
  signal?: AbortSignal,
): Promise<KGCognitivePendingResponse> {
  const params = new URLSearchParams({ board_id: boardId });
  if (options.kgGenerationId) {
    params.set('kg_generation_id', options.kgGenerationId);
  }
  if (options.status) {
    params.set('status', options.status);
  }
  if (typeof options.limit === 'number') {
    params.set('limit', String(options.limit));
  }
  if (typeof options.offset === 'number') {
    params.set('offset', String(options.offset));
  }

  const resp = await fetch(`${BASE}/kg/cognitive-pending?${params.toString()}`, {
    headers: { 'Content-Type': 'application/json' },
    signal,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const detail = err.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message || detail?.code || err.message || `HTTP ${resp.status}`;
    throw new Error(message);
  }
  return resp.json();
}

// ---- KG-03.6 cognitive badges (api_28a22fec + api_49fce0e1) ------------

export type KGCognitiveBadgeReason =
  | 'active_cognitive_item'
  | 'terminal_status'
  | 'ineligible_entity_type'
  | 'not_found';

export type KGEntityCardType =
  | 'spec'
  | 'refinement'
  | 'task'
  | 'test'
  | 'bug'
  | 'ideation'
  | 'decision'
  | 'other';

export const KG_BADGE_LABEL_ACTIVE = 'Pending cognitive consolidation';

export const KG_BADGE_ELIGIBLE_ENTITY_TYPES: readonly KGEntityCardType[] = [
  'spec',
  'decision',
  'refinement',
  'task',
  'test',
  'bug',
];

export interface KGCognitivePendingBadgeView {
  show_badge: boolean;
  label: string;
  status: KGCognitiveItemStatus | null;
  item_id: string | null;
  updated_at: string | null;
  reason: KGCognitiveBadgeReason | string;
}

export interface KGCognitivePendingBadgesResponse {
  board_id: string;
  selected_kg_generation_id: string | null;
  readonly: true;
  eligible_entity_types: KGEntityCardType[];
  badges: Record<string, KGCognitivePendingBadgeView>;
}

export interface GetKGCognitivePendingBadgesOptions {
  kgGenerationId?: string | null;
}

/**
 * Batch read model for first-line entity card badges
 * (api_28a22fec). One HTTP request per list/context — callers MUST NOT
 * invoke this once per card.
 *
 * The server derives the eligible entity type from the ``<type>:<id>``
 * source_ref prefix. Refinement is semantic-only; spec/task/test/bug
 * are deterministic + semantic cognitive entities.
 */
export async function getKGCognitivePendingBadges(
  boardId: string,
  sourceRefs: string[],
  options: GetKGCognitivePendingBadgesOptions = {},
  signal?: AbortSignal,
): Promise<KGCognitivePendingBadgesResponse> {
  const params = new URLSearchParams();
  params.set('board_id', boardId);
  for (const ref of sourceRefs) {
    params.append('source_refs', ref);
  }
  if (options.kgGenerationId) {
    params.set('kg_generation_id', options.kgGenerationId);
  }
  const resp = await fetch(
    `${BASE}/kg/cognitive-pending/badges?${params.toString()}`,
    {
      headers: { 'Content-Type': 'application/json' },
      signal,
    },
  );
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const detail = err.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message || detail?.code || err.message || `HTTP ${resp.status}`;
    throw new Error(message);
  }
  return resp.json();
}
