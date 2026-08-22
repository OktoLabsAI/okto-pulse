import type {
  SemanticPolicyRenderTelemetry,
} from '@/components/policy-compliance/semanticPolicyModel';

export const POLICY_COMPLIANCE_RENDER_LABELS = [
  'contract_version',
  'outcome',
] as const;

const samples: SemanticPolicyRenderTelemetry[] = [];
const MAX_POLICY_COMPLIANCE_RENDER_SAMPLES = 512;

/** In-process bounded counter stream; no assessment payload is accepted. */
export function recordPolicyComplianceRender(
  sample: SemanticPolicyRenderTelemetry,
): void {
  if (sample.metric !== 'pulse_policy_compliance_render_total') return;
  samples.push({
    metric: 'pulse_policy_compliance_render_total',
    labels: {
      contract_version: sample.labels.contract_version,
      outcome: sample.labels.outcome,
    },
  });
  if (samples.length > MAX_POLICY_COMPLIANCE_RENDER_SAMPLES) {
    samples.splice(0, samples.length - MAX_POLICY_COMPLIANCE_RENDER_SAMPLES);
  }
}

export function getPolicyComplianceRenderSamples(): SemanticPolicyRenderTelemetry[] {
  return samples.map((sample) => ({
    metric: sample.metric,
    labels: { ...sample.labels },
  }));
}

export function resetPolicyComplianceRenderTelemetry(): void {
  samples.length = 0;
}
