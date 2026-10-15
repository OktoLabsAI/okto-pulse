import type { SpecStructuredEntityMutationResult } from '@/types';

/** Preserve the native structured-entity code across result and HTTP responses. */
export function specEntityMutationError(result: SpecStructuredEntityMutationResult, fallback: string): Error {
  return Object.assign(new Error(result.error_message || fallback), { code: result.error_code });
}

export function isSpecEntityVersionConflict(cause: unknown): boolean {
  if (!cause || typeof cause !== 'object') return false;
  const details = 'details' in cause && cause.details && typeof cause.details === 'object' ? cause.details : null;
  const code = ('code' in cause ? cause.code : null)
    ?? (details && 'error_code' in details ? details.error_code : null);
  return code === 'version_conflict';
}
