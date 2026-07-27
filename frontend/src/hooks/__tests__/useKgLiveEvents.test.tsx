import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthenticatedFetch } from '@/lib/authFetch';
import { KgLiveEvent, useKgLiveEvents } from '../useKgLiveEvents';

const apiContext = vi.hoisted(() => ({
  apiClient: null as unknown as AuthenticatedFetch,
  isReady: true,
}));

vi.mock('@/contexts/ApiContext', () => ({
  useApiContext: () => ({
    apiClient: apiContext.apiClient,
    getFreshToken: vi.fn(),
    getToken: vi.fn(),
    isReady: apiContext.isReady,
  }),
}));

interface ControllableSse {
  response: Response;
  emit: (chunk: string) => void;
  close: () => void;
}

function controllableSse(): ControllableSse {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(value) {
      controller = value;
    },
  });
  return {
    response: new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }),
    emit: (chunk) => controller.enqueue(encoder.encode(chunk)),
    close: () => controller.close(),
  };
}

async function flushPromises(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 6; i += 1) {
      await Promise.resolve();
    }
  });
}

describe('useKgLiveEvents', () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    vi.useFakeTimers();
    originalFetch = globalThis.fetch;
    apiContext.isReady = true;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('uses the authenticated API transport and refreshes a rejected token', async () => {
    const stream = controllableSse();
    let cachedToken = 'stale-token';
    const getToken = vi.fn(async (options?: { skipCache?: boolean }) => {
      if (options?.skipCache) cachedToken = 'fresh-token';
      return cachedToken;
    });
    const authorizationHeaders: Array<string | null> = [];
    const fetchMock = vi.fn(async (url: string, options?: RequestInit) => {
      authorizationHeaders.push(new Headers(options?.headers).get('Authorization'));
      if (authorizationHeaders.at(-1) === 'Bearer stale-token') {
        return new Response('', { status: 401 });
      }
      if (url.includes('/events/poll')) {
        return new Response(JSON.stringify({
          events: [],
          progress: { pending: 0, total: 0 },
          cursor: '2026-07-27T11:59:59Z',
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return stream.response;
    });
    globalThis.fetch = fetchMock as typeof fetch;
    apiContext.apiClient = new AuthenticatedFetch(getToken, '/api/v1');

    const { result, unmount } = renderHook(() => useKgLiveEvents('board-1'));
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0][0])
      .toBe('/api/v1/kg/boards/board-1/events/poll?limit=500');
    expect(fetchMock.mock.calls[2][0])
      .toBe('/api/v1/kg/boards/board-1/events?since=2026-07-27T11%3A59%3A59Z');
    expect(authorizationHeaders).toEqual([
      'Bearer stale-token',
      'Bearer fresh-token',
      'Bearer fresh-token',
    ]);
    expect(new Headers(fetchMock.mock.calls[2][1]?.headers).get('Accept'))
      .toBe('text/event-stream');

    await act(async () => {
      stream.emit('event: hello\ndata: {}\n\n');
      await Promise.resolve();
    });
    expect(result.current.connectionState).toBe('connected');
    unmount();
  });

  it('parses split SSE frames, tracks progress, and debounces commit bursts', async () => {
    const stream = controllableSse();
    const apiClient = {
      fetch: vi.fn().mockResolvedValue(stream.response),
      fetchJson: vi.fn().mockResolvedValue({
        events: [],
        progress: { pending: 0, total: 0 },
        cursor: '2026-07-27T11:59:59Z',
      }),
    };
    apiContext.apiClient = apiClient as unknown as AuthenticatedFetch;
    const onFlush = vi.fn();
    const { result, unmount } = renderHook(() => (
      useKgLiveEvents('board/with spaces', { onFlush })
    ));
    await flushPromises();

    await act(async () => {
      stream.emit('event: hello\r\ndata: {}\r\n\r\n');
      stream.emit(
        'event: kg.queue.progress\ndata: {"event_id":"p1","event_type":"kg.queue.progress",'
        + '"created_at":"2026-07-27T12:00:00Z","payload":{"pending":2,'
        + '"claimed":1,"done":4,"failed":0,"paused":0,"total":7}}\n\n',
      );
      stream.emit(
        'event: kg.session.committed\ndata: {"event_id":"e1","session_id":"s1",',
      );
      stream.emit(
        '"event_type":"kg.session.committed","created_at":"2026-07-27T12:00:01Z"}\n\n'
        + 'event: kg.board.cleared\ndata: {"event_id":"e2","session_id":"s2",'
        + '"event_type":"kg.board.cleared","created_at":"2026-07-27T12:00:02Z"}\n\n',
      );
      await Promise.resolve();
    });

    expect(apiClient.fetch).toHaveBeenCalledWith(
      '/kg/boards/board%2Fwith%20spaces/events?since=2026-07-27T11%3A59%3A59Z',
      expect.objectContaining({
        cache: 'no-store',
        signal: expect.any(AbortSignal),
      }),
    );
    expect(result.current.connectionState).toBe('connected');
    expect(result.current.queueProgress).toMatchObject({
      pending: 2,
      claimed: 1,
      processed: 4,
      total: 7,
    });
    expect(result.current.unseenCommits).toBe(2);
    expect(result.current.lastEvent?.event_id).toBe('e2');
    expect(onFlush).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(600);
    });
    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush.mock.calls[0][0].map((event: { event_id: string }) => event.event_id))
      .toEqual(['e1', 'e2']);
    const streamSignal = apiClient.fetch.mock.calls[0][1].signal as AbortSignal;
    act(() => {
      vi.advanceTimersByTime(4_000);
    });
    expect(streamSignal.aborted).toBe(false);
    act(() => result.current.markSeen());
    expect(result.current.unseenCommits).toBe(0);
    unmount();
  });

  it('keeps the composite cursor monotonic across non-consecutive replays', async () => {
    const firstStream = controllableSse();
    const secondStream = controllableSse();
    const apiClient = {
      fetch: vi.fn()
        .mockResolvedValueOnce(firstStream.response)
        .mockResolvedValueOnce(secondStream.response),
      fetchJson: vi.fn().mockResolvedValue({
        events: [],
        progress: { pending: 0, total: 0 },
        cursor: '2026-07-27T12:00:00.123455+00:00',
        cursor_event_id: 'baseline',
      }),
    };
    apiContext.apiClient = apiClient as unknown as AuthenticatedFetch;
    const onFlush = vi.fn();
    const { result, unmount } = renderHook(() => (
      useKgLiveEvents('board-replay', { onFlush })
    ));
    await flushPromises();

    await act(async () => {
      firstStream.emit('event: hello\ndata: {}\n\n');
      firstStream.emit(
        'event: kg.session.committed\ndata: {"event_id":"e1",'
        + '"event_type":"kg.session.committed",'
        + '"created_at":"2026-07-27T12:00:00.123456+00:00"}\n\n',
      );
      firstStream.emit(
        'event: kg.board.cleared\ndata: {"event_id":"e2",'
        + '"event_type":"kg.board.cleared",'
        + '"created_at":"2026-07-27T12:00:00.123456Z"}\n\n',
      );
      firstStream.emit(
        'event: kg.session.committed\ndata: {"event_id":"e1",'
        + '"event_type":"kg.session.committed",'
        + '"created_at":"2026-07-27T12:00:00.123456+00:00"}\n\n',
      );
      firstStream.emit(
        'event: kg.session.committed\ndata: {"event_id":"e9",'
        + '"event_type":"kg.session.committed",'
        + '"created_at":"2026-07-27T12:00:00.123455+00:00"}\n\n',
      );
      firstStream.emit(
        'event: kg.session.committed\ndata: {"event_id":"e3",'
        + '"event_type":"kg.session.committed","created_at":null}\n\n',
      );
      await Promise.resolve();
    });

    expect(result.current.unseenCommits).toBe(2);
    expect(result.current.lastEvent?.event_id).toBe('e2');

    await act(async () => {
      firstStream.close();
      await Promise.resolve();
      await Promise.resolve();
      vi.advanceTimersByTime(1_000);
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });

    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush.mock.calls[0][0].map((event: KgLiveEvent) => event.event_id))
      .toEqual(['e1', 'e2']);
    expect(apiClient.fetch).toHaveBeenNthCalledWith(
      2,
      '/kg/boards/board-replay/events?since=2026-07-27T12%3A00%3A00.123456Z'
      + '&after_event_id=e2',
      expect.any(Object),
    );
    unmount();
  });

  it('preserves commits during a pre-first-event outage and reconnects', async () => {
    const reconnectedStream = controllableSse();
    const apiClient = {
      fetch: vi.fn()
        .mockRejectedValueOnce(new Error('stream failure 1'))
        .mockRejectedValueOnce(new Error('stream failure 2'))
        .mockRejectedValueOnce(new Error('stream failure 3'))
        .mockResolvedValueOnce(reconnectedStream.response),
      fetchJson: vi.fn()
        .mockResolvedValueOnce({
          events: [],
          progress: {
            pending: 1,
            claimed: 0,
            done: 0,
            failed: 0,
            paused: 0,
            total: 1,
          },
          cursor: '2026-07-27T12:00:00Z',
          cursor_event_id: 'baseline-0',
        })
        .mockResolvedValueOnce({
          events: [{
            event_id: 'polled-1',
            session_id: null,
            event_type: 'kg.session.committed',
            created_at: '2026-07-27T12:00:01Z',
          }],
          progress: {
            pending: 0,
            claimed: 0,
            done: 1,
            failed: 0,
            paused: 0,
            total: 1,
          },
          cursor: '2026-07-27T12:00:01Z',
          cursor_event_id: 'polled-1',
        }),
    };
    apiContext.apiClient = apiClient as unknown as AuthenticatedFetch;
    const onFlush = vi.fn();

    const { result, unmount } = renderHook(() => (
      useKgLiveEvents('board-2', { onFlush })
    ));
    await flushPromises();

    expect(apiClient.fetchJson).toHaveBeenNthCalledWith(
      1,
      '/kg/boards/board-2/events/poll?limit=500',
      { signal: expect.any(AbortSignal) },
    );
    expect(apiClient.fetch).toHaveBeenNthCalledWith(
      1,
      '/kg/boards/board-2/events?since=2026-07-27T12%3A00%3A00Z'
      + '&after_event_id=baseline-0',
      expect.any(Object),
    );

    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(apiClient.fetchJson).toHaveBeenNthCalledWith(
      2,
      '/kg/boards/board-2/events/poll?since=2026-07-27T12%3A00%3A00Z'
      + '&after_event_id=baseline-0&limit=500',
      { signal: expect.any(AbortSignal) },
    );
    expect(result.current.unseenCommits).toBe(1);
    expect(result.current.lastEvent?.event_id).toBe('polled-1');
    expect(result.current.lastEvent?.session_id).toBeNull();
    expect(apiClient.fetch).toHaveBeenLastCalledWith(
      '/kg/boards/board-2/events?since=2026-07-27T12%3A00%3A01Z'
      + '&after_event_id=polled-1',
      expect.any(Object),
    );
    act(() => {
      vi.advanceTimersByTime(600);
    });
    expect(onFlush).toHaveBeenCalledTimes(1);
    expect(onFlush.mock.calls[0][0]).toMatchObject([{ event_id: 'polled-1' }]);
    unmount();
  });

  it('waits for ApiProvider readiness and aborts the stream on unmount', async () => {
    const stream = controllableSse();
    const apiClient = {
      fetch: vi.fn().mockResolvedValue(stream.response),
      fetchJson: vi.fn().mockResolvedValue({
        events: [],
        progress: { pending: 0, total: 0 },
        cursor: '2026-07-27T12:00:00Z',
      }),
    };
    apiContext.apiClient = apiClient as unknown as AuthenticatedFetch;
    apiContext.isReady = false;

    const { rerender, unmount } = renderHook(() => useKgLiveEvents('board-3'));
    expect(apiClient.fetch).not.toHaveBeenCalled();
    expect(apiClient.fetchJson).not.toHaveBeenCalled();

    apiContext.isReady = true;
    rerender();
    await flushPromises();
    expect(apiClient.fetchJson).toHaveBeenCalledTimes(1);
    const signal = apiClient.fetch.mock.calls[0][1].signal as AbortSignal;
    expect(signal.aborted).toBe(false);

    unmount();
    expect(signal.aborted).toBe(true);
  });

  it('aborts a stuck baseline poll after 4s and allows the retry', async () => {
    const stream = controllableSse();
    let firstSignal: AbortSignal | null = null;
    const fetchJson = vi.fn()
      .mockImplementationOnce((_url: string, options?: RequestInit) => (
        new Promise((_resolve, reject) => {
          firstSignal = options?.signal ?? null;
          firstSignal?.addEventListener(
            'abort',
            () => reject(new Error('poll aborted')),
            { once: true },
          );
        })
      ))
      .mockResolvedValueOnce({
        events: [],
        progress: { pending: 0, total: 0 },
        cursor: '2026-07-27T12:00:00Z',
      });
    const apiClient = {
      fetch: vi.fn().mockResolvedValue(stream.response),
      fetchJson,
    };
    apiContext.apiClient = apiClient as unknown as AuthenticatedFetch;

    const { unmount } = renderHook(() => useKgLiveEvents('board-timeout'));
    await flushPromises();
    expect(fetchJson).toHaveBeenCalledTimes(1);
    expect((firstSignal as AbortSignal | null)?.aborted).toBe(false);

    await act(async () => {
      vi.advanceTimersByTime(4_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect((firstSignal as AbortSignal | null)?.aborted).toBe(true);

    await act(async () => {
      vi.advanceTimersByTime(1_000);
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });

    expect(fetchJson).toHaveBeenCalledTimes(2);
    expect(apiClient.fetch).toHaveBeenCalledWith(
      '/kg/boards/board-timeout/events?since=2026-07-27T12%3A00%3A00Z',
      expect.any(Object),
    );
    unmount();
  });

  it('aborts and retries when the stream fetch never resolves', async () => {
    const retryStream = controllableSse();
    let pendingSignal: AbortSignal | null = null;
    const fetchStream = vi.fn()
      .mockImplementationOnce((_url: string, options?: RequestInit) => (
        new Promise((_resolve, reject) => {
          pendingSignal = options?.signal ?? null;
          pendingSignal?.addEventListener(
            'abort',
            () => reject(new Error('stream fetch aborted')),
            { once: true },
          );
        })
      ))
      .mockResolvedValueOnce(retryStream.response);
    const apiClient = {
      fetch: fetchStream,
      fetchJson: vi.fn().mockResolvedValue({
        events: [],
        progress: { pending: 0, total: 0 },
        cursor: '2026-07-27T12:00:00Z',
        cursor_event_id: 'baseline',
      }),
    };
    apiContext.apiClient = apiClient as unknown as AuthenticatedFetch;

    const { unmount } = renderHook(() => useKgLiveEvents('board-fetch-timeout'));
    await flushPromises();
    expect(fetchStream).toHaveBeenCalledTimes(1);
    expect((pendingSignal as AbortSignal | null)?.aborted).toBe(false);

    await act(async () => {
      vi.advanceTimersByTime(4_000);
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
    expect((pendingSignal as AbortSignal | null)?.aborted).toBe(true);

    await act(async () => {
      vi.advanceTimersByTime(1_000);
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
    expect(fetchStream).toHaveBeenCalledTimes(2);
    unmount();
  });

  it('aborts and retries when a 200 stream never sends hello', async () => {
    const silentStream = controllableSse();
    const retryStream = controllableSse();
    const fetchStream = vi.fn()
      .mockResolvedValueOnce(silentStream.response)
      .mockResolvedValueOnce(retryStream.response);
    const apiClient = {
      fetch: fetchStream,
      fetchJson: vi.fn().mockResolvedValue({
        events: [],
        progress: { pending: 0, total: 0 },
        cursor: '2026-07-27T12:00:00Z',
        cursor_event_id: 'baseline',
      }),
    };
    apiContext.apiClient = apiClient as unknown as AuthenticatedFetch;

    const { result, unmount } = renderHook(() => (
      useKgLiveEvents('board-hello-timeout')
    ));
    await flushPromises();
    const silentSignal = fetchStream.mock.calls[0][1].signal as AbortSignal;
    expect(silentSignal.aborted).toBe(false);

    await act(async () => {
      vi.advanceTimersByTime(4_000);
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
    expect(silentSignal.aborted).toBe(true);
    expect(result.current.connectionState).toBe('disconnected');

    await act(async () => {
      vi.advanceTimersByTime(1_000);
      for (let i = 0; i < 6; i += 1) await Promise.resolve();
    });
    expect(fetchStream).toHaveBeenCalledTimes(2);
    unmount();
  });
});
