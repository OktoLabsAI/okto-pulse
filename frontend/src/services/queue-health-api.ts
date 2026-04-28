/**
 * API client for the consolidation queue health endpoint (spec bdcda842).
 *
 * Polled by the EventQueueTab inside RuntimeSettingsPanel every 2000ms
 * while the tab is active. Backend mirror: core/api/queue_health.py.
 */

export interface QueueHealth {
  queue_depth: number;
  oldest_pending_age_s: number;
  claimed_count: number;
  claimed_boards: string[];
  dead_letter_count: number;
  claims_per_min_1m: number;
  claims_per_min_5m: number;
  alert_threshold: number;
  alert_active: boolean;
  alert_fired_total: number;
  workers_active: number;
  workers_idle: number;
  workers_draining_count: number;
  kuzu_lock_retries_5m: number;
}

const BASE = '/api/v1';

export async function getQueueHealth(
  signal?: AbortSignal,
): Promise<QueueHealth> {
  const resp = await fetch(`${BASE}/kg/queue/health`, {
    headers: { 'Content-Type': 'application/json' },
    signal,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
  }
  return resp.json();
}
