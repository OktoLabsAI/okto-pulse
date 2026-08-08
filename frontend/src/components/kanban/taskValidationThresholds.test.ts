import { describe, expect, it } from 'vitest';

import { resolveTaskValidationThresholds } from './taskValidationThresholds';

describe('resolveTaskValidationThresholds', () => {
  it('resolves each field independently through sprint, spec, and board', () => {
    expect(resolveTaskValidationThresholds({
      boardSettings: {
        min_confidence: 70,
        min_completeness: 80,
        max_drift: 12,
      },
      spec: {
        validation_min_confidence: 88,
        validation_min_completeness: 92,
      },
      sprint: {
        validation_min_confidence: 95,
        validation_min_completeness: null,
      },
    })).toEqual({
      min_confidence: 95,
      min_completeness: 92,
      max_drift: 12,
      resolved_sources: {
        min_confidence: 'sprint',
        min_completeness: 'spec',
        max_drift: 'board',
      },
    });
  });

  it('uses backend defaults only after every override layer is nullish', () => {
    expect(resolveTaskValidationThresholds({
      boardSettings: null,
      spec: {
        validation_min_confidence: null,
        validation_min_completeness: undefined,
        validation_max_drift: null,
      },
      sprint: {},
    })).toEqual({
      min_confidence: 70,
      min_completeness: 80,
      max_drift: 50,
      resolved_sources: {
        min_confidence: 'default',
        min_completeness: 'default',
        max_drift: 'default',
      },
    });
  });

  it('preserves zero as a configured threshold', () => {
    const thresholds = resolveTaskValidationThresholds({
      boardSettings: {
        min_confidence: 70,
        min_completeness: 80,
        max_drift: 50,
      },
      sprint: { validation_max_drift: 0 },
    });

    expect(thresholds.max_drift).toBe(0);
    expect(thresholds.resolved_sources.max_drift).toBe('sprint');
  });
});
