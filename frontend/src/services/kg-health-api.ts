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
  contradict_warn_count: number;
  last_decay_tick_at: string | null;
  nodes_recomputed_in_last_tick: number;
  // True quando o advisory lock global ``kg_daily_tick`` está acquired —
  // serve para desabilitar o botão "Run tick now" mesmo após remount do
  // componente (single source of truth via backend).
  tick_in_progress?: boolean;
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
