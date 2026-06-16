// Metrics Publish Health (R5C-D). Consumes ONLY the already-redacted publish-health
// DTO from the core endpoint (R5C-A/E). The UI never fetches raw state and never
// recomputes status — `status` from this DTO is the source of truth.

export type PublishHealthStatus =
  | 'disabled'
  | 'healthy'
  | 'degraded'
  | 'recovering'
  | 'failing'
  | 'stale'
  | 'unavailable';

export type PublishHealthSeverity = 'none' | 'info' | 'warning' | 'critical';

export const HEALTH_SOURCE_UNAVAILABLE = 'HEALTH_SOURCE_UNAVAILABLE';

export interface PublishHealthFreshness {
  last_success_at: string | null;
  age_seconds: number | null;
  is_stale: boolean;
  stale_threshold_seconds: number;
}

export interface PublishHealthSource {
  name: string;
  status: string;
  severity: string;
  reason_category: string;
  message: string;
  available: boolean;
  last_success_at: string | null;
}

// Fields are intentionally permissive: the panel must tolerate null / missing /
// unknown-extra fields without breaking. Only the redacted DTO ever reaches here.
export interface PublishHealth {
  status: string;
  source?: string;
  severity?: string;
  reason_code?: string | null;
  reason_category?: string;
  http_status?: number | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  next_retry_at?: string | null;
  retry_count?: number;
  freshness?: PublishHealthFreshness | null;
  install_id_redacted?: string | null;
  message?: string;
  sources?: PublishHealthSource[];
  redaction_applied?: boolean;
  // present on the structured HEALTH_SOURCE_UNAVAILABLE response (HTTP 503)
  error?: string;
}

const BASE = '/api/v1';

// Returns the redacted DTO for both the 200 health response and the structured
// 503 HEALTH_SOURCE_UNAVAILABLE body (the latter is rendered as an unavailable
// state, NOT thrown). Only a non-JSON / transport failure throws.
export async function getPublishHealth(signal?: AbortSignal): Promise<PublishHealth> {
  const resp = await fetch(`${BASE}/metrics/publish-health`, {
    headers: { 'Content-Type': 'application/json' },
    signal,
  });
  const body = await resp.json().catch(() => null);
  if (body && typeof body === 'object') {
    return body as PublishHealth;
  }
  throw new Error(`Failed to load publish health (HTTP ${resp.status})`);
}
