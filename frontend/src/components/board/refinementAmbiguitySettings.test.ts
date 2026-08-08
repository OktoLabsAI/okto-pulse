import { describe, expect, it } from 'vitest';

import {
  DEFAULT_REFINEMENT_AMBIGUITY_THRESHOLD,
  normalizeRefinementAmbiguityThreshold,
} from './refinementAmbiguitySettings';

describe('refinement ambiguity threshold normalization', () => {
  it('defaults missing or malformed values to 3', () => {
    expect(DEFAULT_REFINEMENT_AMBIGUITY_THRESHOLD).toBe(3);
    expect(normalizeRefinementAmbiguityThreshold(undefined)).toBe(3);
    expect(normalizeRefinementAmbiguityThreshold(Number.NaN)).toBe(3);
    expect(normalizeRefinementAmbiguityThreshold('5')).toBe(3);
  });

  it('clamps and integer-normalizes persisted values to the 1..5 contract', () => {
    expect(normalizeRefinementAmbiguityThreshold(0)).toBe(1);
    expect(normalizeRefinementAmbiguityThreshold(2.9)).toBe(2);
    expect(normalizeRefinementAmbiguityThreshold(6)).toBe(5);
  });
});
