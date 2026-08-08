export const DEFAULT_REFINEMENT_AMBIGUITY_THRESHOLD = 3;

export function normalizeRefinementAmbiguityThreshold(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return DEFAULT_REFINEMENT_AMBIGUITY_THRESHOLD;
  }
  return Math.min(5, Math.max(1, Math.trunc(value)));
}
