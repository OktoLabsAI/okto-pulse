import { renderHook } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { useBoardRuntimeActivation } from './useBoardRuntimeActivation';

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
