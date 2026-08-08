import type { SpecStatus } from '@/types';

const VALIDATION_STAGE_STATUSES: ReadonlySet<SpecStatus> = new Set([
  'approved',
  'validated',
  'in_progress',
  'done',
]);

export function isSpecValidationAvailable(status: SpecStatus): boolean {
  return VALIDATION_STAGE_STATUSES.has(status);
}
