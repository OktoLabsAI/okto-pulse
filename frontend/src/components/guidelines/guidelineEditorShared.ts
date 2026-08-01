import type { PolicyEntityType } from '@/types/policy-governance';

export const GUIDELINE_ENTITY_TYPES: readonly PolicyEntityType[] = [
  'ideation',
  'refinement',
  'spec',
  'sprint',
  'card',
  'test_scenario',
];

let fallbackId = 0;

export function createGuidelineClientId(prefix: string): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  if (randomUuid) return `${prefix}-${randomUuid}`;
  fallbackId += 1;
  return `${prefix}-${Date.now()}-${fallbackId}`;
}
