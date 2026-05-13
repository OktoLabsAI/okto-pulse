export type MetricsMode = 'disabled' | 'local_only' | 'anonymous_beacon';

export const CURRENT_METRICS_SCHEMA_VERSION = '1.1.0';

export interface MetricsSummary {
  mode: MetricsMode;
  source: string;
  metrics_dir: string;
  retention_days: number;
  schema_version: string;
  product_aggregate_families: string[];
  summary: {
    event_count: number;
    by_event_type: Record<string, number>;
    by_day: Record<string, number>;
    files_count: number;
  };
  beacon_status: {
    enabled: boolean;
    last_handshake_at: string | null;
    last_send_at: string | null;
    circuit_open_until: string | null;
    schema_status: string;
  };
  next_opt_in_prompt_after: string | null;
  consent: {
    source: string | null;
    changed_at: string | null;
    policy_version: string | null;
    schema_version: string | null;
  };
  resolved_precedence: string[];
}

const BASE = '/api/v1';

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const detail = typeof err?.detail === 'string' ? err.detail : err?.message || resp.statusText;
    throw new Error(detail);
  }
  return resp.json();
}

export async function getMetricsSummary(windowDays = 30): Promise<MetricsSummary> {
  const resp = await fetch(`${BASE}/metrics/local/summary?window_days=${windowDays}`);
  return jsonOrThrow<MetricsSummary>(resp);
}

export async function updateMetricsMode(
  mode: MetricsMode,
  acknowledgedItems: string[] = [],
): Promise<{ mode: MetricsMode; changed_at: string; schema_version: string | null; next_opt_in_prompt_after: string | null }> {
  const resp = await fetch(`${BASE}/metrics/settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode,
      source: 'settings_ui',
      policy_version: mode === 'anonymous_beacon' ? '2026-05-11' : undefined,
      schema_version: mode === 'anonymous_beacon' ? CURRENT_METRICS_SCHEMA_VERSION : undefined,
      acknowledged_items: acknowledgedItems,
    }),
  });
  return jsonOrThrow(resp);
}

export async function exportLocalMetrics(): Promise<{ output_path: string; exported: boolean }> {
  const resp = await fetch(`${BASE}/metrics/local/export`, { method: 'POST' });
  return jsonOrThrow(resp);
}

export async function purgeLocalMetrics(): Promise<{ purged_files: number; purged_at: string }> {
  const resp = await fetch(`${BASE}/metrics/local`, { method: 'DELETE' });
  return jsonOrThrow(resp);
}
