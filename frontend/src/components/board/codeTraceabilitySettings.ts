import type { CodeTraceabilitySettings } from '@/types';

export const DEFAULT_CODE_TRACEABILITY_SETTINGS: CodeTraceabilitySettings = {
  mode: 'advisory',
  evidence_attestation: 'preferred',
  target_resolution: 'advisory',
  accepted_attestor_policy: 'granular_permission',
  minimum_trust: 'single_attestation',
  preflight_freshness_seconds: 1800,
  overlap_policy: 'warn',
  observed_state_policy: 'allow_dirty_attestation',
  receipt_content: 'safe_excerpt',
};

const CODE_TRACEABILITY_ENFORCEMENT_MODES = [
  'advisory',
  'blocking',
] as const;

export type CodeTraceabilityEnforcementMode =
  (typeof CODE_TRACEABILITY_ENFORCEMENT_MODES)[number];

function normalizeCodeTraceabilityEnforcementMode(
  value: unknown,
): CodeTraceabilityEnforcementMode {
  return CODE_TRACEABILITY_ENFORCEMENT_MODES.includes(
    value as CodeTraceabilityEnforcementMode,
  )
    ? (value as CodeTraceabilityEnforcementMode)
    : 'advisory';
}

export function normalizeCodeTraceabilitySettings(
  value: unknown,
): CodeTraceabilitySettings {
  const persisted = value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Partial<CodeTraceabilitySettings>)
    : {};
  return {
    ...DEFAULT_CODE_TRACEABILITY_SETTINGS,
    ...persisted,
    // `off` was accepted by earlier releases. Keep the response type compatible
    // with those payloads, but never project or resubmit a silent/no-guidance
    // mode through the Board or Global Default configuration UI.
    mode: normalizeCodeTraceabilityEnforcementMode(persisted.mode),
  };
}
