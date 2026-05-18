/**
 * Spec f64d1aaf — Optional, consent-aware telemetry adapter for guided help.
 *
 * The adapter is INJECTED into GuidedHelpProvider via the `telemetryAdapter`
 * prop. It is intentionally minimal:
 *   - Honours three modes resolved at emit time: `disabled` (no-op),
 *     `local_only` (POST to the local events endpoint) and
 *     `anonymous_beacon` (same payload reaches the beacon path via the
 *     same endpoint; backend decides aggregation).
 *   - Only sends a categorical, allowlisted payload — never IDs, paths,
 *     URLs or free text. See `br_d125f1a5 Telemetry privacy`.
 *   - Errors are swallowed so the UI flow never blocks on telemetry.
 *
 * Tests in ``telemetry.test.ts`` cover ts_63744afc (modes + payload allowlist).
 */

import type {
  GuidedHelpTelemetryAdapter,
  GuidedHelpTelemetryEvent,
} from './types';

export type GuidedHelpTelemetryMode = 'disabled' | 'local_only' | 'anonymous_beacon';

/** Categorical fields allowed to leave the browser. Anything else is dropped. */
export const GUIDED_HELP_TELEMETRY_PAYLOAD_KEYS = [
  'action',
  'tour_surface',
  'step_kind',
  'status',
  'duration_ms',
] as const;

type AllowedKey = (typeof GUIDED_HELP_TELEMETRY_PAYLOAD_KEYS)[number];

export interface ConsentAwareTelemetryAdapterDeps {
  /** Resolves the current metrics mode at emit time. */
  getMode: () => GuidedHelpTelemetryMode | Promise<GuidedHelpTelemetryMode>;
  /** Performs the network send. Defaults to fetch POST /api/v1/metrics/local/events. */
  send?: (payload: SanitizedGuidedHelpTelemetryPayload) => Promise<void>;
  /** Optional sink for tests/observability that observes every sanitized payload. */
  onEmit?: (payload: SanitizedGuidedHelpTelemetryPayload, mode: GuidedHelpTelemetryMode) => void;
}

export interface SanitizedGuidedHelpTelemetryPayload {
  schema_version: '1.0';
  event_type: 'guided_help';
  payload: Pick<GuidedHelpTelemetryEvent, AllowedKey>;
}

const ALLOWED_KEY_SET = new Set<string>(GUIDED_HELP_TELEMETRY_PAYLOAD_KEYS);

export function sanitizeGuidedHelpEvent(
  event: GuidedHelpTelemetryEvent,
): SanitizedGuidedHelpTelemetryPayload {
  const payload: Partial<Record<AllowedKey, unknown>> = {};
  const eventRecord = event as unknown as Record<string, unknown>;
  for (const key of Object.keys(event)) {
    if (ALLOWED_KEY_SET.has(key)) {
      const typedKey = key as AllowedKey;
      payload[typedKey] = eventRecord[key];
    }
  }
  return {
    schema_version: '1.0',
    event_type: 'guided_help',
    payload: payload as Pick<GuidedHelpTelemetryEvent, AllowedKey>,
  };
}

async function defaultSend(payload: SanitizedGuidedHelpTelemetryPayload): Promise<void> {
  if (typeof fetch !== 'function') return;
  try {
    await fetch('/api/v1/metrics/local/events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      keepalive: true,
    });
  } catch {
    // Telemetry must never block the UI. Swallow network errors.
  }
}

export function createConsentAwareTelemetryAdapter(
  deps: ConsentAwareTelemetryAdapterDeps,
): GuidedHelpTelemetryAdapter {
  const send = deps.send ?? defaultSend;
  return {
    emit: async (event) => {
      let mode: GuidedHelpTelemetryMode;
      try {
        mode = await deps.getMode();
      } catch {
        return; // Treat mode resolution failures as disabled.
      }
      if (mode === 'disabled') {
        return;
      }
      const sanitized = sanitizeGuidedHelpEvent(event);
      deps.onEmit?.(sanitized, mode);
      try {
        await send(sanitized);
      } catch {
        // Already swallowed by defaultSend; protect custom senders too.
      }
    },
  };
}
