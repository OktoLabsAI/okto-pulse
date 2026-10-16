/**
 * Shared submit helper for the Q&A answer flows in the Ideation, Refinement and
 * Spec modals.
 *
 * Answering a question is a compare-and-swap against the server's current Q&A
 * state: a concurrent write (another agent answering the same question, or a
 * stale local snapshot) comes back as a retryable 409 conflict. The UX contract
 * is:
 *
 *   - one in-flight request per question (the caller owns that guard),
 *   - on a retryable conflict, refresh the local Q&A list and retry EXACTLY
 *     once,
 *   - never retry a non-retryable failure, and surface the server's own message
 *     instead of a generic one.
 */

import { AuthenticatedFetchError } from '@/lib/authFetch';

export const DEFAULT_ANSWER_ERROR_MESSAGE = 'Failed to post answer';

/**
 * A failure is worth one automatic retry when the server reported a conflict
 * (409) or explicitly flagged the error as retryable.
 */
export function isRetryableAnswerError(error: unknown): boolean {
  return (
    error instanceof AuthenticatedFetchError
    && (error.status === 409 || error.retryable === true)
  );
}

/**
 * Prefer the server's message so the user sees what actually went wrong; fall
 * back to the generic copy only when there is no message to show.
 */
export function answerErrorMessage(
  error: unknown,
  fallback: string = DEFAULT_ANSWER_ERROR_MESSAGE,
): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  if (typeof error === 'string' && error.trim()) {
    return error;
  }
  return fallback;
}

export interface SubmitQaAnswerOptions<T> {
  /** Performs the API call. Invoked at most twice (initial attempt + 1 retry). */
  send: () => Promise<T>;
  /** Refreshes the local Q&A snapshot before the single retry. */
  reload: () => Promise<void>;
  /** Overridable for tests; defaults to {@link isRetryableAnswerError}. */
  isRetryable?: (error: unknown) => boolean;
}

/**
 * Runs `send`, and on a retryable failure reloads the Q&A list and retries once.
 *
 * Resolves with the (possibly retried) result. Rejects with the original error
 * when it is not retryable, and with the retry's error when the retry fails.
 * `reload` failures are never swallowed silently — they reject too.
 */
export async function submitQaAnswerWithRetry<T>({
  send,
  reload,
  isRetryable = isRetryableAnswerError,
}: SubmitQaAnswerOptions<T>): Promise<T> {
  try {
    return await send();
  } catch (error) {
    if (!isRetryable(error)) {
      throw error;
    }
    await reload();
    // Exactly one retry: a second conflict propagates to the caller.
    return await send();
  }
}
