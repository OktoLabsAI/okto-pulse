import { cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import { useBoardRuntimeActivation } from './useBoardRuntimeActivation';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function busyError() {
  return new AuthenticatedFetchError({
    status: 503,
    code: 'board_runtime_busy',
    retryable: true,
    message: 'Graph information is being updated. Please try again shortly.',
  });
}

function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

it('does not activate on render or rerender; explicit same-board actions share one request', async () => {
  const request = deferred();
  const activate = vi.fn(() => request.promise);
  const notify = vi.fn();
  const { result, rerender } = renderHook(() => useBoardRuntimeActivation(activate, () => true, notify));
  rerender();
  expect(activate).not.toHaveBeenCalled();
  const first = result.current('board');
  const second = result.current('board');
  expect(second).toBe(first);
  await Promise.resolve();
  expect(activate).toHaveBeenCalledExactlyOnceWith('board');
  request.resolve();
  expect(await first).toBe(true);
  expect(notify).not.toHaveBeenCalled();
});

it('reports an unavailable graph once and retries only on the next explicit action', async () => {
  const request = deferred();
  const activate = vi.fn().mockReturnValueOnce(request.promise).mockResolvedValue(undefined);
  const notify = vi.fn();
  const { result, rerender } = renderHook(() => useBoardRuntimeActivation(activate, () => true, notify));
  const first = result.current('board');
  const second = result.current('board');
  request.reject(new Error('unavailable'));
  expect(await first).toBe(false);
  expect(await second).toBe(false);
  rerender();
  expect(activate).toHaveBeenCalledTimes(1);
  expect(notify).toHaveBeenCalledTimes(1);
  expect(await result.current('board')).toBe(true);
  expect(activate).toHaveBeenCalledTimes(2);
});

it('allows independent Board activation and suppresses an old Board failure', async () => {
  const old = deferred();
  let selected = 'old';
  const notify = vi.fn();
  const activate = vi.fn((id: string) => id === 'old' ? old.promise : Promise.resolve());
  const { result } = renderHook(() => useBoardRuntimeActivation(activate, (id) => selected === id, notify));
  const previous = result.current('old');
  selected = 'new';
  expect(await result.current('new')).toBe(true);
  old.reject(new Error('old failure'));
  expect(await previous).toBe(false);
  expect(notify).not.toHaveBeenCalled();
  expect(activate.mock.calls).toEqual([['old'], ['new']]);
});

it('does not notify after unmount and still owns the pending result', async () => {
  const request = deferred();
  const notify = vi.fn();
  const { result, unmount } = renderHook(() => useBoardRuntimeActivation(() => request.promise, () => true, notify));
  const pending = result.current('board');
  unmount();
  request.reject(new Error('unavailable'));
  expect(await pending).toBe(false);
  expect(notify).not.toHaveBeenCalled();
});

it('handles a synchronous transport failure without an unhandled rejection', async () => {
  const notify = vi.fn();
  const { result } = renderHook(() => useBoardRuntimeActivation(() => { throw new Error('transport'); }, () => true, notify));
  expect(await result.current('board')).toBe(false);
  expect(notify).toHaveBeenCalledOnce();
});

it('recovers from typed busy after one second and deduplicates throughout the retry', async () => {
  vi.useFakeTimers();
  const activate = vi.fn().mockRejectedValueOnce(busyError()).mockResolvedValue(undefined);
  const notify = vi.fn();
  const progress = vi.fn();
  const { result } = renderHook(() => useBoardRuntimeActivation(activate, () => true, notify, progress));
  const request = result.current('board');
  await vi.advanceTimersByTimeAsync(0);
  expect(activate).toHaveBeenCalledTimes(1);
  expect(progress).toHaveBeenCalledExactlyOnceWith('board', true);
  expect(result.current('board')).toBe(request);
  await vi.advanceTimersByTimeAsync(999);
  expect(activate).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(await request).toBe(true);
  expect(activate).toHaveBeenCalledTimes(2);
  expect(progress.mock.calls).toEqual([['board', true], ['board', false]]);
  expect(notify).not.toHaveBeenCalled();
});

it('exhausts typed busy after three attempts and reports it separately from unavailable', async () => {
  vi.useFakeTimers();
  const activate = vi.fn().mockRejectedValue(busyError());
  const notify = vi.fn();
  const progress = vi.fn();
  const { result } = renderHook(() => useBoardRuntimeActivation(activate, () => true, notify, progress));
  const request = result.current('board');
  await vi.advanceTimersByTimeAsync(1000);
  expect(activate).toHaveBeenCalledTimes(2);
  expect(result.current('board')).toBe(request);
  await vi.advanceTimersByTimeAsync(1000);
  expect(await request).toBe(false);
  expect(activate).toHaveBeenCalledTimes(3);
  expect(notify).toHaveBeenCalledExactlyOnceWith('busy');
  expect(progress.mock.calls).toEqual([['board', true], ['board', false]]);
  await vi.advanceTimersByTimeAsync(10000);
  expect(activate).toHaveBeenCalledTimes(3);
});

it('cancels the busy delay and clears progress when the selected Board changes', async () => {
  vi.useFakeTimers();
  let selected = 'old';
  const activate = vi.fn().mockRejectedValueOnce(busyError()).mockResolvedValue(undefined);
  const notify = vi.fn();
  const progress = vi.fn();
  const { result, rerender } = renderHook(() => useBoardRuntimeActivation(activate, (id) => id === selected, notify, progress));
  const request = result.current('old');
  await vi.advanceTimersByTimeAsync(0);
  selected = 'new';
  rerender();
  expect(await request).toBe(false);
  expect(progress.mock.calls).toEqual([['old', true], ['old', false]]);
  expect(await result.current('new')).toBe(true);
  await vi.advanceTimersByTimeAsync(5000);
  expect(activate.mock.calls).toEqual([['old'], ['new']]);
  expect(notify).not.toHaveBeenCalled();
});

it('cancels the busy delay and clears progress on unmount', async () => {
  vi.useFakeTimers();
  const activate = vi.fn().mockRejectedValue(busyError());
  const notify = vi.fn();
  const progress = vi.fn();
  const { result, unmount } = renderHook(() => useBoardRuntimeActivation(activate, () => true, notify, progress));
  const request = result.current('board');
  await vi.advanceTimersByTimeAsync(0);
  unmount();
  expect(await request).toBe(false);
  expect(progress.mock.calls).toEqual([['board', true], ['board', false]]);
  await vi.advanceTimersByTimeAsync(5000);
  expect(activate).toHaveBeenCalledTimes(1);
  expect(notify).not.toHaveBeenCalled();
});

it('ignores a late busy response from an in-flight retry after a Board switch', async () => {
  vi.useFakeTimers();
  const late = deferred();
  let selected = 'old';
  const activate = vi.fn().mockRejectedValueOnce(busyError()).mockReturnValueOnce(late.promise);
  const notify = vi.fn();
  const progress = vi.fn();
  const { result, rerender } = renderHook(() => useBoardRuntimeActivation(activate, (id) => id === selected, notify, progress));
  const request = result.current('old');
  await vi.advanceTimersByTimeAsync(1000);
  expect(activate).toHaveBeenCalledTimes(2);
  selected = 'new';
  rerender();
  late.reject(busyError());
  expect(await request).toBe(false);
  await vi.advanceTimersByTimeAsync(5000);
  expect(activate).toHaveBeenCalledTimes(2);
  expect(progress.mock.calls).toEqual([['old', true], ['old', false]]);
  expect(notify).not.toHaveBeenCalled();
});

it('does not announce or retry a busy response received after unmount', async () => {
  vi.useFakeTimers();
  const late = deferred();
  const activate = vi.fn(() => late.promise);
  const notify = vi.fn();
  const progress = vi.fn();
  const { result, unmount } = renderHook(() => useBoardRuntimeActivation(activate, () => true, notify, progress));
  const request = result.current('board');
  await vi.advanceTimersByTimeAsync(0);
  unmount();
  late.reject(busyError());
  expect(await request).toBe(false);
  await vi.advanceTimersByTimeAsync(5000);
  expect(activate).toHaveBeenCalledTimes(1);
  expect(progress).not.toHaveBeenCalled();
  expect(notify).not.toHaveBeenCalled();
});

it.each([
  ['other 503', new AuthenticatedFetchError({ status: 503, code: 'graph_unavailable', retryable: true, message: 'Unavailable' })],
  ['non-retryable busy', new AuthenticatedFetchError({ status: 503, code: 'board_runtime_busy', retryable: false, message: 'Unavailable' })],
  ['forbidden', new AuthenticatedFetchError({ status: 403, code: 'board_runtime_busy', retryable: true, message: 'Denied' })],
  ['unauthorized', new AuthenticatedFetchError({ status: 401, code: 'board_runtime_busy', retryable: true, message: 'Denied' })],
  ['server error', new AuthenticatedFetchError({ status: 500, code: 'board_runtime_busy', retryable: true, message: 'Failure' })],
  ['network error', new TypeError('Failed to fetch')],
  ['untyped error', Object.assign(new Error('Busy'), { status: 503, code: 'board_runtime_busy', retryable: true })],
])('does not retry %s', async (_name, error) => {
  vi.useFakeTimers();
  const activate = vi.fn().mockRejectedValue(error);
  const notify = vi.fn();
  const progress = vi.fn();
  const { result } = renderHook(() => useBoardRuntimeActivation(activate, () => true, notify, progress));
  expect(await result.current('board')).toBe(false);
  await vi.advanceTimersByTimeAsync(5000);
  expect(activate).toHaveBeenCalledTimes(1);
  expect(notify).toHaveBeenCalledExactlyOnceWith();
  expect(progress).not.toHaveBeenCalled();
});
