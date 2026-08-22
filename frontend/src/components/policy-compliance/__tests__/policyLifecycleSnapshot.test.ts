import { describe, expect, it } from 'vitest';

import type { ValidationCycleCheckSummary } from '@/types';
import { parsePolicyComplianceLifecycleDetails } from '../policyLifecycleSnapshot';

function policyCheck(): ValidationCycleCheckSummary {
  return {
    result_type: 'policy_compliance',
    status: 'passed',
    summary: 'All applicable policies passed',
    details: {
      counts: {
        applicable: 1,
        completed: 1,
        passed: 1,
        failed: 0,
        waived: 0,
        skipped: 0,
        pending: 0,
        context_only: 2,
        inconsistent: 0,
        scope_inconsistent: 0,
        blocking: 1,
        advisory: 0,
        blocking_failed: 0,
        blocking_pending: 0,
        advisory_failed: 0,
        advisory_pending: 0,
        failed_metrics: 0,
        waived_metrics: 0,
        unwaived_failed_metrics: 0,
      },
      applicable_bindings: [{
        binding_id: 'binding-frozen',
        guideline_id: 'guideline-frozen',
        revision_id: 'revision-frozen',
        title: 'Frozen policy',
        enforcement: 'blocking',
        minimum_confidence: 80,
        status: 'passed',
        failed_metric_count: 0,
        waived_metric_count: 0,
        unwaived_failed_metric_count: 0,
        metrics: [{
          metric_id: 'metric-frozen',
          code: 'quality.frozen',
          title: 'Frozen metric',
          description: 'Frozen description.',
          description_truncated: false,
          evaluation_rubric: 'Frozen rubric.',
          evaluation_rubric_truncated: false,
          assessment_outcome: 'passed',
          direction: 'minimum',
          default_threshold: 70,
          effective_threshold: 75,
          threshold_source: 'override',
        }],
      }],
    },
  };
}

describe('parsePolicyComplianceLifecycleDetails', () => {
  it('accepts a bounded frozen policy projection', () => {
    expect(parsePolicyComplianceLifecycleDetails(policyCheck())).toMatchObject({
      counts: { applicable: 1, context_only: 2 },
      applicable_bindings: [{
        binding_id: 'binding-frozen',
        revision_id: 'revision-frozen',
        metrics: [{ effective_threshold: 75 }],
      }],
    });
  });

  it('rejects a live-denominator-shaped payload whose count exceeds frozen applicable bindings', () => {
    const check = policyCheck();
    const details = check.details as Record<string, unknown>;
    check.details = {
      ...details,
      counts: {
        ...(details.counts as Record<string, unknown>),
        applicable: 5,
      },
    };

    expect(parsePolicyComplianceLifecycleDetails(check)).toBeNull();
  });

  it('rejects enforcement counters that contradict the frozen bindings', () => {
    const check = policyCheck();
    const details = check.details as Record<string, unknown>;
    check.details = {
      ...details,
      counts: {
        ...(details.counts as Record<string, unknown>),
        blocking: 0,
        advisory: 1,
      },
    };

    expect(parsePolicyComplianceLifecycleDetails(check)).toBeNull();
  });

  it('accepts a mixed snapshot with verified bindings and separate scope inconsistency', () => {
    const check = policyCheck();
    const details = check.details as Record<string, unknown>;
    check.details = {
      ...details,
      counts: {
        ...(details.counts as Record<string, unknown>),
        scope_inconsistent: 1,
      },
    };

    expect(parsePolicyComplianceLifecycleDetails(check)).toMatchObject({
      counts: { applicable: 1, scope_inconsistent: 1 },
      applicable_bindings: [{ binding_id: 'binding-frozen' }],
    });
  });

  it('accepts a fully waived binding and preserves its metric outcome', () => {
    const check = policyCheck();
    const details = check.details as {
      counts: Record<string, number>;
      applicable_bindings: Array<Record<string, unknown>>;
    };
    const binding = details.applicable_bindings[0];
    const metrics = binding.metrics as Array<Record<string, unknown>>;
    binding.status = 'waived';
    binding.failed_metric_count = 1;
    binding.waived_metric_count = 1;
    binding.unwaived_failed_metric_count = 0;
    metrics[0].assessment_outcome = 'waived';
    Object.assign(details.counts, {
      passed: 0,
      waived: 1,
      failed_metrics: 1,
      waived_metrics: 1,
      unwaived_failed_metrics: 0,
    });

    expect(parsePolicyComplianceLifecycleDetails(check)).toMatchObject({
      counts: { waived: 1, unwaived_failed_metrics: 0 },
      applicable_bindings: [{
        status: 'waived',
        metrics: [{ assessment_outcome: 'waived' }],
      }],
    });
  });

  it('rejects a waived binding whose failed finding remains uncovered', () => {
    const check = policyCheck();
    const details = check.details as {
      counts: Record<string, number>;
      applicable_bindings: Array<Record<string, unknown>>;
    };
    const binding = details.applicable_bindings[0];
    const metrics = binding.metrics as Array<Record<string, unknown>>;
    binding.status = 'waived';
    binding.failed_metric_count = 1;
    binding.waived_metric_count = 0;
    binding.unwaived_failed_metric_count = 1;
    metrics[0].assessment_outcome = 'failed';
    Object.assign(details.counts, {
      passed: 0,
      waived: 1,
      failed_metrics: 1,
      waived_metrics: 0,
      unwaived_failed_metrics: 1,
    });

    expect(parsePolicyComplianceLifecycleDetails(check)).toBeNull();
  });
});
