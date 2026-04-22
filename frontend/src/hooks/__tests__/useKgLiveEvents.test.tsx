import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useKgLiveEvents } from '../useKgLiveEvents';

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  url: string;
  readyState = 0;
  withCredentials = false;
  CONNECTING = 0 as const;
  OPEN = 1 as const;
  CLOSED = 2 as const;
  onopen: ((ev: Event) => unknown) | null = null;
  onmessage: ((ev: MessageEvent) => unknown) | null = null;
  onerror: ((ev: Event) => unknown) | null = null;
  private listeners = new Map<string, Set<(ev: MessageEvent) => void>>();
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, cb: (ev: MessageEvent) => void): void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(cb);
  }
  removeEventListener(type: string, cb: (ev: MessageEvent) => void): void {
    this.listeners.get(type)?.delete(cb);
  }
  close(): void {
    this.closed = true;
    this.readyState = this.CLOSED;
  }
  dispatchEvent(_ev: Event): boolean {
    return true;
  }

  /** Test helper — emit an event of `type` carrying `data`. */
  emit(type: string, data: unknown): void {
    const cbs = this.listeners.get(type);
    if (!cbs) return;
    const evt = { data: typeof data === 'string' ? data : JSON.stringify(data) } as MessageEvent;
    cbs.forEach((cb) => cb(evt));
  }

  /** Fire onerror to simulate a connection failure. */
  fail(): void {
    this.onerror?.(new Event('error'));
  }
}

describe('useKgLiveEvents', () => {
  let originalES: typeof EventSource | undefined;

  beforeEach(() => {
    vi.useFakeTimers();
    originalES = (globalThis as { EventSource?: typeof EventSource }).EventSource;
    (globalThis as { EventSource?: unknown }).EventSource = FakeEventSource as unknown as typeof EventSource;
    FakeEventSource.instances = [];
  });

  afterEach(() => {
    if (originalES) {
      (globalThis as { EventSource?: unknown }).EventSource = originalES;
    } else {
      delete (globalThis as { EventSource?: unknown }).EventSource;
    }
    vi.useRealTimers();
  });

  it('opens an EventSource and transitions to connected on hello', async () => {
    const { result } = renderHook(() => useKgLiveEvents('board-1'));
    expect(FakeEventSource.instances.length).toBe(1);
    act(() => FakeEventSource.instances[0].emit('hello', {}));
    expect(result.current.connectionState).toBe('connected');
  });

  it('debounces commit bursts and flushes once', async () => {
    const onFlush = vi.fn();
    const { result } = renderHook(() => useKgLiveEvents('b', { onFlush }));
    const es = FakeEventSource.instances[0];
    act(() => es.emit('hello', {}));

    act(() => {
      es.emit('kg.session.committed', { event_id: 'e1', session_id: 's1', event_type: 'kg.session.committed', created_at: null });
      es.emit('kg.session.committed', { event_id: 'e2', session_id: 's1', event_type: 'kg.session.committed', created_at: null });
      es.emit('kg.session.committed', { event_id: 'e3', session_id: 's1', event_type: 'kg.session.committed', created_at: null });
    });

    expect(result.current.unseenCommits).toBe(3);
    expect(onFlush).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(600);
    });
    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush.mock.calls[0][0].length).toBe(3);
  });

  it('markSeen resets unseen count without flushing', () => {
    const { result } = renderHook(() => useKgLiveEvents('b'));
    const es = FakeEventSource.instances[0];
    act(() => es.emit('hello', {}));
    act(() => es.emit('kg.session.committed', { event_id: 'x', session_id: 's', event_type: 'kg.session.committed', created_at: null }));
    expect(result.current.unseenCommits).toBe(1);
    act(() => result.current.markSeen());
    expect(result.current.unseenCommits).toBe(0);
  });

  it('falls back to polling after MAX_CONSECUTIVE_FAILURES', () => {
    // Stub fetch so the polling loop's first tick doesn't blow up.
    const originalFetch = global.fetch;
    global.fetch = vi.fn().mockResolvedValue(new Response('', { status: 200 })) as typeof fetch;

    const { result } = renderHook(() => useKgLiveEvents('b'));

    // Three failed attempts in succession.
    for (let i = 0; i < 3; i += 1) {
      const es = FakeEventSource.instances[FakeEventSource.instances.length - 1];
      act(() => es.fail());
      // For attempts 1 and 2 the hook schedules a quick retry (1s * n).
      if (i < 2) {
        act(() => {
          vi.advanceTimersByTime(2_000 * (i + 1));
        });
      }
    }

    expect(result.current.connectionState).toBe('polling');
    global.fetch = originalFetch;
  });

  it('closes the EventSource on unmount', () => {
    const { unmount } = renderHook(() => useKgLiveEvents('b'));
    const es = FakeEventSource.instances[0];
    unmount();
    expect(es.closed).toBe(true);
  });
});
