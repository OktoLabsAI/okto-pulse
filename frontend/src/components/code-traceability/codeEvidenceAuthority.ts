import type { CodeTraceabilityProjection } from '@/types';

/**
 * The classification transaction consumes server-issued CAS values. Keep
 * those inputs out of every client-side projection that is not separately
 * authorized for the classification action, whether the caller is a human
 * operator or an authenticated agent.
 */
export function sanitizeCodeEvidenceProjectionForAuthority(
  projection: CodeTraceabilityProjection,
  canClassifyLegacyEvidence: boolean,
): CodeTraceabilityProjection {
  if (canClassifyLegacyEvidence) return projection;
  return {
    ...projection,
    source_context_classification_inputs: [],
  };
}
