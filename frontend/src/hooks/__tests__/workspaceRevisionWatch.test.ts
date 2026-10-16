import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { WorkspaceRevisions, WorkspaceSubjectRef } from '@/types';
import { WorkspaceRevisionWatch } from '../workspaceRevisionWatch';

const spec: WorkspaceSubjectRef = { subject_type: 'spec', subject_id: 'spec' };
const card: WorkspaceSubjectRef = { subject_type: 'card', subject_id: 'card' };
const cleanups: (() => void)[] = [];
function reply(subjects: WorkspaceSubjectRef[], revision = 1, authority = 1): WorkspaceRevisions {
  return { board_id: 'board', authority: { incarnation: 'a'.repeat(32), revision: authority },
    subjects: subjects.map(subject => ({ ...subject, token: { incarnation: 'b'.repeat(32), revision } })) };
}
function subscribe(watch: WorkspaceRevisionWatch, subjects = [spec]) {
  const callbacks = { onChange: vi.fn(), onState: vi.fn(), onAccessLost: vi.fn() };
  const unsubscribe = watch.subscribe({ subjects, ...callbacks });
  cleanups.push(unsubscribe);
  return { ...callbacks, unsubscribe };
}
beforeEach(() => {
  vi.useFakeTimers();
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
});
afterEach(() => {
  cleanups.splice(0).forEach(dispose => dispose());
  vi.useRealTimers();
});

describe('shared workspace revision watcher', () => {
  it('deduplicates consumers and notifies only changed subjects after the initial sync', async () => {
    let cardRevision = 1;
    const read = vi.fn(async (_board: string, subjects: WorkspaceSubjectRef[]) => {
      const result = reply(subjects);
      result.subjects.forEach(item => { if (item.subject_type === 'card') item.token!.revision = cardRevision; });
      return result;
    });
    const watch = new WorkspaceRevisionWatch('board', read);
    const first = subscribe(watch);
    const second = subscribe(watch, [spec, card]);
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(1);
    expect(read.mock.calls[0][1]).toEqual([spec, card]);
    first.onChange.mockClear(); second.onChange.mockClear();
    await vi.advanceTimersByTimeAsync(5000);
    expect(first.onChange).not.toHaveBeenCalled();
    expect(second.onChange).not.toHaveBeenCalled();
    cardRevision = 2;
    await vi.advanceTimersByTimeAsync(5000);
    expect(first.onChange).not.toHaveBeenCalled();
    expect(second.onChange).toHaveBeenCalledWith({ subjects: [card], authorityChanged: false, initial: false });
  });

  it('aborts stale requests and ignores late responses after focus revalidation', async () => {
    let resolveOld!: (value: WorkspaceRevisions) => void;
    const read = vi.fn()
      .mockImplementationOnce(() => new Promise<WorkspaceRevisions>(resolve => { resolveOld = resolve; }))
      .mockResolvedValue(reply([spec], 2));
    const subscriber = subscribe(new WorkspaceRevisionWatch('board', read));
    await vi.advanceTimersByTimeAsync(0);
    const signal = read.mock.calls[0][2] as AbortSignal;
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(0);
    expect(signal.aborted).toBe(true);
    expect(subscriber.onChange).toHaveBeenCalledTimes(1);
    resolveOld(reply([spec], 1));
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(5000);
    expect(subscriber.onChange).toHaveBeenCalledTimes(1);
  });

  it('stops while hidden/offline, rechecks immediately on return and disposes the timer', async () => {
    const read = vi.fn(async () => reply([spec]));
    const subscriber = subscribe(new WorkspaceRevisionWatch('board', read));
    await vi.advanceTimersByTimeAsync(0);
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(read).toHaveBeenCalledTimes(1);
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(2);
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: false });
    window.dispatchEvent(new Event('offline'));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(read).toHaveBeenCalledTimes(2);
    expect(subscriber.onState).toHaveBeenLastCalledWith('offline');
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: true });
    window.dispatchEvent(new Event('online'));
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(3);
    subscriber.unsubscribe();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(read).toHaveBeenCalledTimes(3);
  });

  it('backs off technical failures and clears protected views on lost access', async () => {
    const read = vi.fn().mockRejectedValueOnce(new Error('Network unavailable'))
      .mockRejectedValueOnce(new AuthenticatedFetchError({ status: 403, message: 'Denied' }))
      .mockResolvedValue(reply([spec], 3));
    const subscriber = subscribe(new WorkspaceRevisionWatch('board', read));
    await vi.advanceTimersByTimeAsync(0);
    expect(subscriber.onState).toHaveBeenLastCalledWith('unavailable');
    expect(subscriber.onChange).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5000);
    expect(read).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(subscriber.onAccessLost).toHaveBeenCalledTimes(1);
    expect(subscriber.onState).toHaveBeenLastCalledWith('access-lost');
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(0);
    expect(subscriber.onChange).toHaveBeenCalledWith({ subjects: [spec], authorityChanged: false, initial: true });
  });

  it('bounds batches to fifty and rejects inconsistent authority without publishing partial data', async () => {
    let authority = 0;
    const read = vi.fn(async (_board: string, subjects: WorkspaceSubjectRef[]) => reply(subjects, 1, ++authority));
    const subjects = Array.from({ length: 51 }, (_, index) => ({ subject_type: 'spec' as const, subject_id: String(index) }));
    const subscriber = subscribe(new WorkspaceRevisionWatch('board', read), subjects);
    await vi.advanceTimersByTimeAsync(0);
    expect(read.mock.calls.map(call => call[1].length)).toEqual([50, 1]);
    expect(subscriber.onChange).not.toHaveBeenCalled();
    expect(subscriber.onState).toHaveBeenLastCalledWith('unavailable');
  });

  it('notifies unchanged subjects when their Board authority changes', async () => {
    const read = vi.fn().mockResolvedValueOnce(reply([spec])).mockResolvedValue(reply([spec], 1, 2));
    const subscriber = subscribe(new WorkspaceRevisionWatch('board', read));
    await vi.advanceTimersByTimeAsync(0);
    subscriber.onChange.mockClear();
    await vi.advanceTimersByTimeAsync(5000);
    expect(subscriber.onChange).toHaveBeenCalledWith({ subjects: [spec], authorityChanged: true, initial: false });
  });
});
