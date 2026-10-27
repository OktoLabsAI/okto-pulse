import { AuthenticatedFetchError } from '@/lib/authFetch';

export class ReadinessContractError extends Error {}

export function isRetriableReadinessError(error: unknown): boolean {
  if (error instanceof ReadinessContractError) return false;
  if (error instanceof AuthenticatedFetchError)
    return error.status === 408 || error.status === 429 || error.status >= 500;
  return true;
}
