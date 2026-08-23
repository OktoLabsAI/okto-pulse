import { describe, expect, it } from 'vitest';
import type { FlowHealthResponse } from './analyticsCanonicalTypes';
import { deriveFlowHealthMetrics } from './flowHealthMetrics';

function response(overrides: Partial<FlowHealthResponse> = {}): FlowHealthResponse {
  return {
    query_fingerprint: 'f'.repeat(64),
    as_of: '2026-08-22T12:00:00Z',
    effective_policy: {
      version: 1,
      general_stale_hours: 48,
      rejected_stale_hours: 72,
    },
    summary: {},
    items: [],
    ...overrides,
  };
}

describe('deriveFlowHealthMetrics', () => {
  it('does not infer board KPIs from rows when authority is incomplete', () => {
    const data = response({
      summary: { restricted: 1 },
      items: [{
        subject: { type: 'card', id: 'card-1' },
        state: 'rejected',
        reason_codes: [],
        current_episode: { state: 'rejected', age_seconds: 7_200 },
        blockers: [{ code: 'blocked' }],
        rework: [{ completed_at: '2026-08-22T11:00:00Z' }],
        dependency_report: { wait_p50_hours: 4, max_depth: 2 },
        defect_report: { open_bugs: 3, high_severity: 1 },
      }],
    });

    expect(deriveFlowHealthMetrics(data)).toEqual({
      blockerOccurrences: null,
      blockerSubjects: null,
      rejectedWip: null,
      rejectedP95Hours: null,
      recoveryRate: null,
      recoverySample: null,
      dependencyWaitP50Hours: null,
      dependencyDepth: null,
      openBugs: null,
      highSeverityBugs: null,
    });
  });

  it('aggregates complete row reports instead of selecting the first subject', () => {
    const data = response({
      items: [
        {
          subject: { type: 'card', id: 'card-1' },
          state: 'active',
          reason_codes: [],
          current_episode: null,
          rework: [],
          dependency_report: { wait_p50_hours: 2, max_depth: 1 },
          defect_report: { open_bugs: 3, high_severity: 1 },
        },
        {
          subject: { type: 'card', id: 'card-2' },
          state: 'active',
          reason_codes: [],
          current_episode: null,
          rework: [],
          dependency_report: { wait_p50_hours: 8, max_depth: 4 },
          defect_report: { open_bugs: 5, high_severity: 2 },
        },
      ],
    });

    const metrics = deriveFlowHealthMetrics(data);
    expect(metrics.dependencyWaitP50Hours).toBe(2);
    expect(metrics.dependencyDepth).toBe(4);
    expect(metrics.openBugs).toBe(8);
    expect(metrics.highSeverityBugs).toBe(3);
  });

  it('returns N/A for incomplete report aggregates', () => {
    const data = response({
      items: [
        {
          subject: { type: 'card', id: 'card-1' },
          state: 'active',
          reason_codes: [],
          current_episode: null,
          rework: [],
          dependency_report: { wait_p50_hours: 2, max_depth: 1 },
        },
        {
          subject: { type: 'card', id: 'card-2' },
          state: 'active',
          reason_codes: [],
          current_episode: null,
          rework: [],
          dependency_report: { max_depth: 4 },
        },
      ],
    });

    expect(deriveFlowHealthMetrics(data).dependencyWaitP50Hours).toBeNull();
  });
});
