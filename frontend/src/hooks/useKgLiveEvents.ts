/**
 * useKgLiveEvents — subscribes to /api/v1/kg/boards/{id}/events (SSE) through
 * the authenticated API client and surfaces commit notifications to the
 * canvas (spec f33eb9ca, card e17717a6).
 *
 * Behavior:
 *   - Opens an authenticated fetch stream on mount and aborts it on unmount
 *     or boardId change.
 *   - Debounces 500ms on bursts of commits to avoid thrashing the canvas.
 *   - Tracks `unseenCommits` count and exposes `markSeen()` to reset it.
 *   - After 3 consecutive connection failures, falls back to 15s polling
 *     through the finite authenticated JSON endpoint.
 *   - `connectionState` exposes 'connecting' | 'connected' | 'polling' |
 *     'disconnected' so the indicator chip can render the right colour.
 *
 * The hook does NOT itself re-fetch the graph; the consumer wires the
 * supplied `onFlush` callback to its own data-loading function.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useApiContext } from '@/contexts/ApiContext';

const EVENTS_BASE = '/kg/boards';
const DEBOUNCE_MS = 500;
const MAX_CONSECUTIVE_FAILURES = 3;
const POLLING_INTERVAL_MS = 15_000;
const POLLING_TIMEOUT_MS = 4_000;
const STREAM_HANDSHAKE_TIMEOUT_MS = 4_000;
const POLLING_LIMIT = 500;

export type KgConnectionState = 'connecting' | 'connected' | 'polling' | 'disconnected';

export interface KgLiveEvent {
  event_id: string;
  session_id?: string | null;
  event_type: string;
  created_at: string | null;
  payload?: Record<string, unknown>;
}

export interface UseKgLiveEventsOptions {
  /** Auto-flush by invoking this callback when a commit burst settles. */
  onFlush?: (events: KgLiveEvent[]) => void;
  /** Disable the hook (useful for tests). */
  enabled?: boolean;
}

export interface KgQueueProgress {
  pending: number;
  claimed: number;
  done: number;
  processed?: number;
  failed: number;
  paused: number;
  total: number;
}

export interface UseKgLiveEventsReturn {
  connectionState: KgConnectionState;
  unseenCommits: number;
  lastEvent: KgLiveEvent | null;
  /** Last `kg.queue.progress` snapshot, or null if none received yet. */
  queueProgress: KgQueueProgress | null;
  markSeen: () => void;
  flushNow: () => void;
}

interface KgLiveEventsPollResponse {
  events?: KgLiveEvent[];
  progress?: Partial<KgQueueProgress> | null;
  cursor?: string | null;
  cursor_event_id?: string | null;
}

interface SseFrame {
  event: string;
  data: string;
}

interface KgEventPosition {
  createdAt: string;
  timestampKey: string;
  eventId: string | null;
}

/**
 * The KG contract emits UTC-aware ISO timestamps. Keep the fractional seconds
 * as text so ordering does not lose Python's microsecond precision.
 */
function createEventPosition(
  createdAt: string | null | undefined,
  eventId: string | null,
): KgEventPosition | null {
  if (typeof createdAt !== 'string') return null;

  const value = createdAt.trim();
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/.exec(
    value,
  );
  if (!match) return null;

  const [, yearText, monthText, dayText, hourText, minuteText, secondText, fraction = ''] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [
    31,
    leapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  if (
    month < 1
    || month > 12
    || day < 1
    || day > daysInMonth[month - 1]
    || hour > 23
    || minute > 59
    || second > 59
  ) {
    return null;
  }

  return {
    createdAt: value,
    timestampKey: `${yearText}-${monthText}-${dayText}T${hourText}:${minuteText}:${secondText}.${fraction.padEnd(6, '0')}`,
    eventId,
  };
}

function compareEventPositions(left: KgEventPosition, right: KgEventPosition): number {
  if (left.timestampKey !== right.timestampKey) {
    return left.timestampKey < right.timestampKey ? -1 : 1;
  }

  const leftEventId = left.eventId ?? '';
  const rightEventId = right.eventId ?? '';
  if (leftEventId === rightEventId) return 0;
  return leftEventId < rightEventId ? -1 : 1;
}

function parseSseFrame(block: string): SseFrame | null {
  let event = 'message';
  const data: string[] = [];

  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    const separator = line.indexOf(':');
    const field = separator === -1 ? line : line.slice(0, separator);
    let value = separator === -1 ? '' : line.slice(separator + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    if (field === 'event') {
      event = value || 'message';
    } else if (field === 'data') {
      data.push(value);
    }
  }

  if (data.length === 0) return null;
  return { event, data: data.join('\n') };
}

async function consumeSseStream(
  response: Response,
  signal: AbortSignal,
  onFrame: (frame: SseFrame) => void,
): Promise<void> {
  if (!response.body) {
    throw new Error('KG live-events response has no readable stream');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const cancelReader = () => {
    void reader.cancel().catch(() => undefined);
  };
  signal.addEventListener('abort', cancelReader, { once: true });

  const drainFrames = (flushRemainder = false) => {
    while (true) {
      const boundary = /\r?\n\r?\n/.exec(buffer);
      if (!boundary) break;
      const block = buffer.slice(0, boundary.index);
      buffer = buffer.slice(boundary.index + boundary[0].length);
      const frame = parseSseFrame(block);
      if (frame) onFrame(frame);
    }

    if (flushRemainder && buffer.trim()) {
      const frame = parseSseFrame(buffer);
      buffer = '';
      if (frame) onFrame(frame);
    }
  };

  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      drainFrames();
    }
    if (!signal.aborted) {
      buffer += decoder.decode();
      drainFrames(true);
    }
  } finally {
    signal.removeEventListener('abort', cancelReader);
    reader.releaseLock();
  }
}

export function useKgLiveEvents(
  boardId: string,
  options: UseKgLiveEventsOptions = {},
): UseKgLiveEventsReturn {
  const { onFlush, enabled = true } = options;
  const { apiClient, isReady } = useApiContext();

  const [connectionState, setConnectionState] = useState<KgConnectionState>('connecting');
  const [unseenCommits, setUnseenCommits] = useState(0);
  const [lastEvent, setLastEvent] = useState<KgLiveEvent | null>(null);
  const [queueProgress, setQueueProgress] = useState<KgQueueProgress | null>(null);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const burstRef = useRef<KgLiveEvent[]>([]);
  const cursorPositionRef = useRef<KgEventPosition | null>(null);
  const onFlushRef = useRef(onFlush);

  // Always invoke the latest callback identity without retriggering subscribe.
  useEffect(() => {
    onFlushRef.current = onFlush;
  }, [onFlush]);

  useEffect(() => {
    cursorPositionRef.current = null;
    burstRef.current = [];
    setUnseenCommits(0);
    setLastEvent(null);
    setQueueProgress(null);
  }, [boardId]);

  const flushBurst = useCallback(() => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    if (burstRef.current.length === 0) return;
    const events = burstRef.current;
    burstRef.current = [];
    onFlushRef.current?.(events);
  }, []);

  const handleCommit = useCallback((data: KgLiveEvent) => {
    if (
      !data
      || typeof data.event_id !== 'string'
      || data.event_id.length === 0
      || typeof data.event_type !== 'string'
    ) {
      return;
    }

    const nextPosition = createEventPosition(data.created_at, data.event_id);
    const currentPosition = cursorPositionRef.current;
    if (
      !nextPosition
      || (
        currentPosition
        && compareEventPositions(nextPosition, currentPosition) <= 0
      )
    ) {
      return;
    }

    cursorPositionRef.current = nextPosition;
    setLastEvent(data);
    burstRef.current.push(data);
    setUnseenCommits((n) => n + 1);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(flushBurst, DEBOUNCE_MS);
  }, [flushBurst]);

  const applyProgress = useCallback((payload?: Partial<KgQueueProgress> | null) => {
    if (
      !payload
      || typeof payload.pending !== 'number'
      || typeof payload.total !== 'number'
    ) {
      return;
    }

    setQueueProgress((prev) => {
      const next = {
        pending: payload.pending ?? 0,
        claimed: payload.claimed ?? 0,
        done: payload.done ?? 0,
        processed: payload.processed ?? payload.done ?? 0,
        failed: payload.failed ?? 0,
        paused: payload.paused ?? 0,
        total: payload.total ?? 0,
      };
      const previousRemaining = prev ? prev.pending + prev.claimed + prev.paused : 0;
      const stableTotal = previousRemaining > 0
        ? Math.max(next.total, prev?.total ?? 0)
        : next.total;
      const remaining = next.pending + next.claimed + next.paused;
      const inferredProcessed = Math.max(0, stableTotal - remaining);
      return {
        ...next,
        total: stableTotal,
        processed: Math.min(stableTotal, Math.max(next.processed ?? 0, inferredProcessed)),
      };
    });
  }, []);

  useEffect(() => {
    if (!enabled || !boardId) {
      setConnectionState('disconnected');
      return;
    }
    if (!isReady) {
      setConnectionState('connecting');
      return;
    }

    let disposed = false;
    let failures = 0;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let pollingTimer: ReturnType<typeof setInterval> | null = null;
    let streamHandshakeTimer: ReturnType<typeof setTimeout> | null = null;
    let streamController: AbortController | null = null;
    let pollController: AbortController | null = null;
    let pollInFlight = false;

    const eventPath = `${EVENTS_BASE}/${encodeURIComponent(boardId)}/events`;

    const clearRetry = () => {
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    const clearStreamHandshake = () => {
      if (streamHandshakeTimer) {
        clearTimeout(streamHandshakeTimer);
        streamHandshakeTimer = null;
      }
    };

    const stopPolling = () => {
      if (pollingTimer) {
        clearInterval(pollingTimer);
        pollingTimer = null;
      }
      if (pollController) {
        pollController.abort();
        pollController = null;
      }
    };

    const appendCursor = (query: URLSearchParams) => {
      const position = cursorPositionRef.current;
      if (!position) return;

      query.set('since', position.createdAt);
      if (position.eventId) {
        query.set('after_event_id', position.eventId);
      }
    };

    const dispatchLiveEvent = (event: KgLiveEvent) => {
      if (
        event.event_type === 'kg.session.committed'
        || event.event_type === 'kg.board.cleared'
      ) {
        handleCommit(event);
      } else if (event.event_type === 'kg.queue.progress') {
        applyProgress(event.payload as Partial<KgQueueProgress> | undefined);
      }
    };

    const pollOnce = async (): Promise<boolean> => {
      if (disposed || pollInFlight) return false;
      pollInFlight = true;
      const controller = new AbortController();
      pollController = controller;
      const timeout = setTimeout(() => controller.abort(), POLLING_TIMEOUT_MS);
      const hadCursor = Boolean(cursorPositionRef.current);

      try {
        const query = new URLSearchParams();
        appendCursor(query);
        query.set('limit', String(POLLING_LIMIT));
        const snapshot = await apiClient.fetchJson<KgLiveEventsPollResponse>(
          `${eventPath}/poll?${query.toString()}`,
          { signal: controller.signal },
        );
        if (disposed || controller.signal.aborted) return false;

        applyProgress(snapshot.progress);
        const events = Array.isArray(snapshot.events) ? snapshot.events : [];
        for (const event of events) dispatchLiveEvent(event);

        const snapshotEventId = (
          typeof snapshot.cursor_event_id === 'string'
          && snapshot.cursor_event_id.length > 0
        )
          ? snapshot.cursor_event_id
          : null;
        const snapshotPosition = createEventPosition(snapshot.cursor, snapshotEventId);
        const currentPosition = cursorPositionRef.current;
        if (
          snapshotPosition
          && (
            !currentPosition
            || compareEventPositions(snapshotPosition, currentPosition) > 0
          )
        ) {
          cursorPositionRef.current = snapshotPosition;
        }

        const hasCursor = Boolean(cursorPositionRef.current);
        if (hasCursor && (events.length > 0 || !hadCursor)) {
          // A first successful finite read establishes the server-side
          // baseline before opening SSE. Later fallback polls reconnect only
          // after observing activity.
          failures = 0;
          if (pollController === controller) pollController = null;
          stopPolling();
          void connectStream();
        }
        return true;
      } catch {
        /* keep polling silently */
        return false;
      } finally {
        clearTimeout(timeout);
        if (pollController === controller) pollController = null;
        pollInFlight = false;
      }
    };

    const startPolling = () => {
      if (disposed || pollingTimer) return;
      setConnectionState('polling');
      void pollOnce();
      pollingTimer = setInterval(() => {
        void pollOnce();
      }, POLLING_INTERVAL_MS);
    };

    const connectStream = async () => {
      if (disposed) return;
      clearRetry();
      stopPolling();
      streamController?.abort();

      const controller = new AbortController();
      streamController = controller;
      setConnectionState('connecting');
      let handshakeTimedOut = false;
      let handshakeTimer: ReturnType<typeof setTimeout> | null = null;
      const clearThisHandshake = () => {
        if (!handshakeTimer) return;
        clearTimeout(handshakeTimer);
        if (streamHandshakeTimer === handshakeTimer) {
          streamHandshakeTimer = null;
        }
        handshakeTimer = null;
      };
      handshakeTimer = setTimeout(() => {
        if (disposed || streamController !== controller) return;
        handshakeTimedOut = true;
        controller.abort();
      }, STREAM_HANDSHAKE_TIMEOUT_MS);
      streamHandshakeTimer = handshakeTimer;

      const query = new URLSearchParams();
      appendCursor(query);
      const suffix = query.toString();

      try {
        const response = await apiClient.fetch(
          suffix ? `${eventPath}?${suffix}` : eventPath,
          {
            signal: controller.signal,
            cache: 'no-store',
            headers: { Accept: 'text/event-stream' },
          },
        );
        if (!response.ok) {
          throw new Error(`KG live-events stream failed with HTTP ${response.status}`);
        }

        await consumeSseStream(
          response,
          controller.signal,
          ({ event: eventType, data }) => {
            if (disposed || controller.signal.aborted) return;
            if (eventType === 'hello') {
              clearThisHandshake();
              failures = 0;
              setConnectionState('connected');
              return;
            }

            try {
              const parsed = JSON.parse(data) as KgLiveEvent;
              const normalized = {
                ...parsed,
                event_type: parsed.event_type || eventType,
              };
              dispatchLiveEvent(normalized);
            } catch {
              /* malformed event — drop */
            }
          },
        );
        if (handshakeTimedOut) {
          throw new Error('KG live-events handshake timed out');
        }
        if (!disposed && !controller.signal.aborted) {
          throw new Error('KG live-events stream ended unexpectedly');
        }
      } catch {
        if (disposed || (controller.signal.aborted && !handshakeTimedOut)) return;
        failures += 1;
        setConnectionState('disconnected');
        if (failures >= MAX_CONSECUTIVE_FAILURES) {
          startPolling();
        } else {
          retryTimer = setTimeout(() => {
            void connectStream();
          }, 1_000 * failures);
        }
      } finally {
        clearThisHandshake();
        if (streamController === controller) streamController = null;
      }
    };

    const establishBaseline = async () => {
      if (disposed) return;
      setConnectionState('connecting');
      const succeeded = await pollOnce();
      if (disposed || (succeeded && cursorPositionRef.current)) return;

      failures += 1;
      setConnectionState('disconnected');
      if (failures >= MAX_CONSECUTIVE_FAILURES) {
        startPolling();
      } else {
        retryTimer = setTimeout(() => {
          void establishBaseline();
        }, 1_000 * failures);
      }
    };

    if (cursorPositionRef.current) {
      void connectStream();
    } else {
      void establishBaseline();
    }

    return () => {
      disposed = true;
      clearRetry();
      stopPolling();
      clearStreamHandshake();
      streamController?.abort();
      streamController = null;
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
        debounceRef.current = null;
      }
      burstRef.current = [];
    };
  }, [apiClient, applyProgress, boardId, enabled, handleCommit, isReady]);

  const markSeen = useCallback(() => setUnseenCommits(0), []);

  return {
    connectionState,
    unseenCommits,
    lastEvent,
    queueProgress,
    markSeen,
    flushNow: flushBurst,
  };
}
