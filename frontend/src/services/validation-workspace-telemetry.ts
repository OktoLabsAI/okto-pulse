export const VALIDATION_WORKSPACE_INTERACTION_METRIC =
  'pulse_validation_workspace_interaction_latency_ms' as const;

export type ValidationWorkspaceInteraction =
  | 'current_validation'
  | 'validation_check'
  | 'previous_results'
  | 'technical_audit';

export type ValidationWorkspaceInteractionAction = 'expand' | 'collapse';

export interface ValidationWorkspaceInteractionSample {
  metric: typeof VALIDATION_WORKSPACE_INTERACTION_METRIC;
  interaction: ValidationWorkspaceInteraction;
  action: ValidationWorkspaceInteractionAction;
  latency_ms: number;
}

const samples: ValidationWorkspaceInteractionSample[] = [];
const MAX_SAMPLES = 512;

function now(): number {
  return typeof performance !== 'undefined' && typeof performance.now === 'function'
    ? performance.now()
    : Date.now();
}

/**
 * Measures the synchronous interaction path. Result/detail reads happen after
 * expansion and are deliberately excluded so a slow backend cannot freeze the
 * disclosure itself.
 */
export function measureValidationWorkspaceInteraction(
  interaction: ValidationWorkspaceInteraction,
  currentlyExpanded: boolean,
  toggle: () => void,
): void {
  const startedAt = now();
  toggle();
  const latency = Math.max(0, now() - startedAt);
  samples.push({
    metric: VALIDATION_WORKSPACE_INTERACTION_METRIC,
    interaction,
    action: currentlyExpanded ? 'collapse' : 'expand',
    latency_ms: Number(latency.toFixed(3)),
  });
  if (samples.length > MAX_SAMPLES) {
    samples.splice(0, samples.length - MAX_SAMPLES);
  }
}

export function getValidationWorkspaceInteractionSamples(): ValidationWorkspaceInteractionSample[] {
  return samples.map((sample) => ({ ...sample }));
}

export function resetValidationWorkspaceInteractionTelemetry(): void {
  samples.length = 0;
}
