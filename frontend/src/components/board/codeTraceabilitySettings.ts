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

/** Resolve creation defaults; incompatible responses have no editable policy. */
export function resolveCodeTraceabilitySettings(
  value: unknown,
): CodeTraceabilitySettings | null {
  if (value === undefined) return { ...DEFAULT_CODE_TRACEABILITY_SETTINGS };
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  const policy = value as Partial<CodeTraceabilitySettings>;
  if (policy.mode !== undefined && !CODE_TRACEABILITY_ENFORCEMENT_MODES.includes(policy.mode)) {
    return null;
  }
  return { ...DEFAULT_CODE_TRACEABILITY_SETTINGS, ...policy };
}
