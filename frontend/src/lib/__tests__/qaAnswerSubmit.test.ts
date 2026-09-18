import { describe, expect, it, vi } from 'vitest';

import { AuthenticatedFetchError } from '@/lib/authFetch';
import {
  DEFAULT_ANSWER_ERROR_MESSAGE,
  answerErrorMessage,
  isRetryableAnswerError,
  submitQaAnswerWithRetry,
} from '@/lib/qaAnswerSubmit';

function fetchError(overrides: Partial<{
  message: string;
  status: number;
  retryable: boolean;
}> = {}) {
  return new AuthenticatedFetchError({
    message: overrides.message ?? 'conflict',
    status: overrides.status ?? 409,
    retryable: overrides.retryable ?? false,
  });
}

describe('isRetryableAnswerError', () => {
  it('treats a 409 AuthenticatedFetchError as retryable', () => {
    expect(isRetryableAnswerError(fetchError({ status: 409 }))).toBe(true);
  });

  it('treats an explicitly retryable AuthenticatedFetchError as retryable', () => {
    expect(
      isRetryableAnswerError(fetchError({ status: 503, retryable: true })),
    ).toBe(true);
  });

  it('does not treat a plain server error as retryable', () => {
    expect(isRetryableAnswerError(fetchError({ status: 500, message: 'boom' }))).toBe(false);
  });

  it('does not treat a non-AuthenticatedFetchError as retryable', () => {
    expect(isRetryableAnswerError(new Error('boom'))).toBe(false);
    expect(isRetryableAnswerError({ status: 409 })).toBe(false);
    expect(isRetryableAnswerError(undefined)).toBe(false);
  });
});

describe('answerErrorMessage', () => {
  it('surfaces the server message from an AuthenticatedFetchError', () => {
    expect(answerErrorMessage(fetchError({ status: 500, message: 'boom' }))).toBe('boom');
  });

  it('surfaces the message of a plain Error', () => {
    expect(answerErrorMessage(new Error('kaboom'))).toBe('kaboom');
  });

  it('surfaces a thrown string', () => {
    expect(answerErrorMessage('raw failure')).toBe('raw failure');
  });

  it('falls back only when the message is empty or missing', () => {
    expect(answerErrorMessage(new Error(''))).toBe(DEFAULT_ANSWER_ERROR_MESSAGE);
    expect(answerErrorMessage(new Error('   '))).toBe(DEFAULT_ANSWER_ERROR_MESSAGE);
    expect(answerErrorMessage(null)).toBe(DEFAULT_ANSWER_ERROR_MESSAGE);
    expect(answerErrorMessage({})).toBe(DEFAULT_ANSWER_ERROR_MESSAGE);
  });
});

describe('submitQaAnswerWithRetry', () => {
  it('sends once and resolves when the first attempt succeeds', async () => {
    const send = vi.fn().mockResolvedValue('ok');
    const reload = vi.fn().mockResolvedValue(undefined);

    await expect(submitQaAnswerWithRetry({ send, reload })).resolves.toBe('ok');

    expect(send).toHaveBeenCalledTimes(1);
    expect(reload).not.toHaveBeenCalled();
  });

  it('reloads and retries exactly once on a 409 conflict, then resolves', async () => {
    const order: string[] = [];
    const send = vi
      .fn()
      .mockImplementationOnce(async () => {
        order.push('send');
        throw fetchError({ status: 409, message: 'stale answer' });
      })
      .mockImplementationOnce(async () => {
        order.push('send');
        return 'retried';
      });
    const reload = vi.fn().mockImplementation(async () => {
      order.push('reload');
    });

    await expect(submitQaAnswerWithRetry({ send, reload })).resolves.toBe('retried');

    expect(send).toHaveBeenCalledTimes(2);
    expect(reload).toHaveBeenCalledTimes(1);
    // The refresh must happen between the failed attempt and the retry.
    expect(order).toEqual(['send', 'reload', 'send']);
  });

  it('retries once on an explicitly retryable error', async () => {
    const send = vi
      .fn()
      .mockRejectedValueOnce(fetchError({ status: 503, retryable: true }))
      .mockResolvedValueOnce('retried');
    const reload = vi.fn().mockResolvedValue(undefined);

    await expect(submitQaAnswerWithRetry({ send, reload })).resolves.toBe('retried');

    expect(send).toHaveBeenCalledTimes(2);
  });

  it('propagates the retry failure and never sends a third time', async () => {
    const second = fetchError({ status: 409, message: 'still conflicting' });
    const send = vi
      .fn()
      .mockRejectedValueOnce(fetchError({ status: 409, message: 'first conflict' }))
      .mockRejectedValueOnce(second);
    const reload = vi.fn().mockResolvedValue(undefined);

    await expect(submitQaAnswerWithRetry({ send, reload })).rejects.toBe(second);

    expect(send).toHaveBeenCalledTimes(2);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('does not retry a non-retryable failure', async () => {
    const error = fetchError({ status: 500, message: 'boom' });
    const send = vi.fn().mockRejectedValue(error);
    const reload = vi.fn().mockResolvedValue(undefined);

    await expect(submitQaAnswerWithRetry({ send, reload })).rejects.toBe(error);

    expect(send).toHaveBeenCalledTimes(1);
    expect(reload).not.toHaveBeenCalled();
  });

  it('honours a caller-supplied isRetryable predicate', async () => {
    const send = vi.fn().mockRejectedValueOnce(new Error('custom')).mockResolvedValueOnce('ok');
    const reload = vi.fn().mockResolvedValue(undefined);

    await expect(
      submitQaAnswerWithRetry({ send, reload, isRetryable: () => true }),
    ).resolves.toBe('ok');

    expect(send).toHaveBeenCalledTimes(2);
  });

  it('propagates a reload failure without a second send', async () => {
    const reloadError = new Error('reload failed');
    const send = vi.fn().mockRejectedValueOnce(fetchError({ status: 409 }));
    const reload = vi.fn().mockRejectedValue(reloadError);

    await expect(submitQaAnswerWithRetry({ send, reload })).rejects.toBe(reloadError);

    expect(send).toHaveBeenCalledTimes(1);
  });
});
