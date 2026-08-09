import { beforeEach, describe, expect, it } from 'vitest';

import {
  getPolicyComplianceRenderSamples,
  recordPolicyComplianceRender,
  resetPolicyComplianceRenderTelemetry,
} from '../policy-compliance-telemetry';

describe('policy compliance render telemetry', () => {
  beforeEach(() => resetPolicyComplianceRenderTelemetry());

  it('retains only the closed non-sensitive label set', () => {
    recordPolicyComplianceRender({
      metric: 'pulse_policy_compliance_render_total',
      labels: {
        contract_version: 'v2',
        outcome: 'current',
        subject_id: 'must-not-leak',
      },
    } as never);

    expect(getPolicyComplianceRenderSamples()).toEqual([{
      metric: 'pulse_policy_compliance_render_total',
      labels: {
        contract_version: 'v2',
        outcome: 'current',
      },
    }]);
  });

  it('bounds the in-process sample buffer', () => {
    for (let index = 0; index < 520; index += 1) {
      recordPolicyComplianceRender({
        metric: 'pulse_policy_compliance_render_total',
        labels: {
          contract_version: index < 8 ? 'v1' : 'v2',
          outcome: 'current',
        },
      });
    }

    const samples = getPolicyComplianceRenderSamples();
    expect(samples).toHaveLength(512);
    expect(samples[0]?.labels.contract_version).toBe('v2');
  });
});
