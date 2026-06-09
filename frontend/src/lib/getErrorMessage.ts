/**
 * Extracts a human-readable error message from any thrown value.
 *
 * Components should use this instead of casting `(err as Error).message`
 * directly, because catch clauses receive `unknown` in strict TypeScript.
 *
 * The message is expected to be already parsed by authFetch (i.e. the backend
 * `detail` string has been extracted), so the primary case is `err instanceof
 * Error`. The string and fallback branches cover edge-cases where callers
 * throw raw strings or non-Error objects.
 */
export function getErrorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  return 'Something went wrong';
}
