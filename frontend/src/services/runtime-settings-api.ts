/**
 * API client for the runtime settings endpoint shipped in 0.1.4.
 *
 * Backs the "Settings" menu (graph database memory tuning knobs). Reads and writes
 * persist through the backend's settings_service and only take effect on
 * the next process restart because graph database startup settings are constructor-time.
 */

export interface RuntimeSettings {
  // Graph DB tab — restart-required on change.
  kg_kuzu_buffer_pool_mb: number;
  kg_kuzu_max_db_size_gb: number;
  kg_connection_pool_size: number;
  // Event Queue tab — hot-reload (no restart needed).
  // Spec bdcda842 v0.2.0+: 5 new settings exposed by the worker pool.
  kg_queue_max_concurrent_workers: number;
  kg_queue_min_interval_ms: number;
  kg_queue_claim_timeout_s: number;
  kg_queue_max_attempts: number;
  kg_queue_alert_threshold: number;
  // Decay Tick tab — hot-reload via APScheduler.reschedule_job.
  // Spec 54399628 (Wave 2 NC f9732afc).
  kg_decay_tick_interval_minutes: number;
  kg_decay_tick_staleness_days: number;
  kg_decay_tick_max_age_days: number;
  // Toggled APENAS by Graph DB tab changes (graph database startup-time).
  restart_required: boolean;
}

export type RuntimeSettingsPatch = Partial<
  Omit<RuntimeSettings, 'restart_required'>
>;

/**
 * Keys that gate the amber "Restart required" banner. Mudar qualquer um
 * deles requer restart do processo Okto Pulse (graph database settings are
 * constructor-time). Demais keys (kg_queue_*) são hot-reload.
 */
export const GRAPH_DB_KEYS = [
  'kg_kuzu_buffer_pool_mb',
  'kg_kuzu_max_db_size_gb',
  'kg_connection_pool_size',
] as const satisfies ReadonlyArray<keyof RuntimeSettings>;

export const EVENT_QUEUE_KEYS = [
  'kg_queue_max_concurrent_workers',
  'kg_queue_min_interval_ms',
  'kg_queue_claim_timeout_s',
  'kg_queue_max_attempts',
  'kg_queue_alert_threshold',
] as const satisfies ReadonlyArray<keyof RuntimeSettings>;

export const DECAY_TICK_KEYS = [
  'kg_decay_tick_interval_minutes',
  'kg_decay_tick_staleness_days',
  'kg_decay_tick_max_age_days',
] as const satisfies ReadonlyArray<keyof RuntimeSettings>;

const BASE = '/api/v1';

function extractErrorMessage(err: any, fallback: string): string {
  const detail = err?.detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => item?.msg || item?.message)
      .filter(Boolean);
    if (messages.length > 0) return messages.join('; ');
  }
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') {
    const msg = detail.msg || detail.message || detail.error;
    if (typeof msg === 'string' && msg.trim()) return msg;
  }
  if (typeof err?.message === 'string' && err.message.trim()) return err.message;
  return fallback;
}

export async function getRuntimeSettings(): Promise<RuntimeSettings> {
  const resp = await fetch(`${BASE}/settings/runtime`, {
    headers: { 'Content-Type': 'application/json' },
  });
  if (!resp.ok) {
    const err = await resp
      .json()
      .catch(() => ({ detail: resp.statusText }));
    throw new Error(extractErrorMessage(err, `HTTP ${resp.status}`));
  }
  return resp.json();
}

export async function putRuntimeSettings(
  patch: RuntimeSettingsPatch,
): Promise<RuntimeSettings> {
  const resp = await fetch(`${BASE}/settings/runtime`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  if (!resp.ok) {
    const err = await resp
      .json()
      .catch(() => ({ detail: resp.statusText }));
    throw new Error(extractErrorMessage(err, `HTTP ${resp.status}`));
  }
  return resp.json();
}
