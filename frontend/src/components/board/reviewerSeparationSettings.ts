import type { ReviewerSeparationMode } from '@/types';

export const REVIEWER_SEPARATION_MODES = [
  'off',
  'warn',
  'enforce',
] as const satisfies readonly ReviewerSeparationMode[];

export function normalizeReviewerSeparationMode(
  value: unknown,
): ReviewerSeparationMode {
  return REVIEWER_SEPARATION_MODES.includes(value as ReviewerSeparationMode)
    ? (value as ReviewerSeparationMode)
    : 'off';
}
