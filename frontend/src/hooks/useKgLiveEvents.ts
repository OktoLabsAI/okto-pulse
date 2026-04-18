/**
 * useKgLiveEvents — subscribes to /api/v1/kg/boards/{id}/events (SSE) and
 * surfaces commit notifications to the canvas (spec f33eb9ca, card e17717a6).
 *
 * Behavior:
 *   - Opens an EventSource on mount, closes it on unmount or boardId change.
 *   - Debounces 500ms on bursts of commits to avoid thrashing the canvas.
 *   - Tracks `unseenCommits` count and exposes `markSeen()` to reset it.
 *   - After 3 consecutive connection failures, falls back to 15s polling
 *     via setInterval. The polling tick increments unseenCommits when the
 *     last_event_id from the server differs from the previous tick.
 *   - `connectionState` exposes 'connecting' | 'connected' | 'polling' |
 *     'disconnected' so the indicator chip can render the right colour.
 *
 * The hook does NOT itself re-fetch the graph; the consumer wires the
 * supplied `onFlush` callback to its own data-loading function.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

const SSE_BASE = '/api/v1/kg/boards';
const DEBOUNCE_MS = 500;
const MAX_CONSECUTIVE_FAILURES = 3;
const POLLING_INTERVAL_MS = 15_000;

export type KgConnectionState = 'connecting' | 'connected' | 'polling' | 'disconnected';

export interface KgLiveEvent {
  event_id: string;
  session_id: string;
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

export interface UseKgLiveEventsReturn {
  connectionState: KgConnectionState;
  unseenCommits: number;
  lastEvent: KgLiveEvent | null;
  markSeen: () => void;
  flushNow: () => void;
}

export function useKgLiveEvents(
  boardId: string,
  options: UseKgLiveEventsOptions = {},
): UseKgLiveEventsReturn {
  const { onFlush, enabled = true } = options;

  const [connectionState, setConnectionState] = useState<KgConnectionState>('connecting');
  const [unseenCommits, setUnseenCommits] = useState(0);
  const [lastEvent, setLastEvent] = useState<KgLiveEvent | null>(null);

  const sourceRef = useRef<EventSource | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const burstRef = useRef<KgLiveEvent[]>([]);
  const failuresRef = useRef(0);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastEventIdRef = useRef<string | null>(null);
  const sinceRef = useRef<string | null>(null);
  const onFlushRef = useRef(onFlush);

  // Always invoke the latest callback identity without retriggering subscribe.
  useEffect(() => {
    onFlushRef.current = onFlush;
  }, [onFlush]);

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

  const handleCommit = useCallback((evt: MessageEvent) => {
    try {
      const data: KgLiveEvent = JSON.parse(evt.data);
      lastEventIdRef.current = data.event_id;
      sinceRef.current = data.created_at ?? sinceRef.current;
      setLastEvent(data);
      burstRef.current.push(data);
      setUnseenCommits((n) => n + 1);
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(flushBurst, DEBOUNCE_MS);
    } catch {
      /* malformed event — drop */
    }
  }, [flushBurst]);

  const startPolling = useCallback(() => {
    if (pollingRef.current) return;
    setConnectionState('polling');
    pollingRef.current = setInterval(async () => {
      try {
        const url = sinceRef.current
          ? `${SSE_BASE}/${boardId}/events?since=${encodeURIComponent(sinceRef.current)}`
          : `${SSE_BASE}/${boardId}/events`;
        // SSE endpoint emits text/event-stream; for a polling read we use
        // a single fetch and parse the chunk for event_ids — we only care
        // about whether something happened so we can bump unseenCommits.
        const ctl = new AbortController();
        const tid = setTimeout(() => ctl.abort(), 4_000);
        const resp = await fetch(url, { signal: ctl.signal });
        clearTimeout(tid);
        if (!resp.ok) return;
        const text = (await resp.text()).slice(0, 5_000);
        const ids = Array.from(text.matchAll(/"event_id"\s*:\s*"([^"]+)"/g)).map((m) => m[1]);
        if (ids.length === 0) return;
        const newest = ids[ids.length - 1];
        if (newest && newest !== lastEventIdRef.current) {
          lastEventIdRef.current = newest;
          setUnseenCommits((n) => n + ids.length);
          // After successful poll, attempt to restore SSE.
          stopPolling();
          subscribe();
        }
      } catch {
        /* keep polling silently */
      }
    }, POLLING_INTERVAL_MS);
  }, [boardId]);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  const subscribe = useCallback(() => {
    if (typeof EventSource === 'undefined') {
      // SSR / older test runners — degrade to polling.
      startPolling();
      return;
    }
    if (sourceRef.current) sourceRef.current.close();
    setConnectionState('connecting');
    const url = sinceRef.current
      ? `${SSE_BASE}/${boardId}/events?since=${encodeURIComponent(sinceRef.current)}`
      : `${SSE_BASE}/${boardId}/events`;
    const es = new EventSource(url);
    sourceRef.current = es;

    es.addEventListener('hello', () => {
      setConnectionState('connected');
      failuresRef.current = 0;
    });
    es.addEventListener('kg.session.committed', handleCommit as EventListener);
    es.addEventListener('kg.board.cleared', handleCommit as EventListener);

    es.onerror = () => {
      failuresRef.current += 1;
      setConnectionState('disconnected');
      es.close();
      sourceRef.current = null;
      if (failuresRef.current >= MAX_CONSECUTIVE_FAILURES) {
        startPolling();
      } else {
        // Quick retry with backoff.
        setTimeout(subscribe, 1_000 * failuresRef.current);
      }
    };
  }, [boardId, handleCommit, startPolling]);

  // Subscribe on mount / boardId change.
  useEffect(() => {
    if (!enabled || !boardId) return;
    subscribe();
    return () => {
      stopPolling();
      if (sourceRef.current) {
        sourceRef.current.close();
        sourceRef.current = null;
      }
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
        debounceRef.current = null;
      }
    };
  }, [boardId, enabled, subscribe, stopPolling]);

  const markSeen = useCallback(() => setUnseenCommits(0), []);

  return {
    connectionState,
    unseenCommits,
    lastEvent,
    markSeen,
    flushNow: flushBurst,
  };
}
