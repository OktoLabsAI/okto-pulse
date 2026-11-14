import { useCallback, useEffect, useRef } from 'react';
import { AuthenticatedFetchError } from '@/lib/authFetch';

const MAX_ATTEMPTS = 3;
const BUSY_RETRY_DELAY_MS = 1000;

function isRetryableBusy(error: unknown): boolean {
  return error instanceof AuthenticatedFetchError
    && error.status === 503
    && error.code === 'board_runtime_busy'
    && error.retryable;
}

/** Explicit actions only. A late graph failure must not replace Board content. */
export function useBoardRuntimeActivation(
  activate: (boardId: string) => Promise<unknown>,
  isCurrentBoard: (boardId: string) => boolean,
  onUnavailable: (reason?: 'busy') => void,
  onBusyChange?: (boardId: string, busy: boolean) => void,
) {
  const pending = useRef(new Map<string, { promise: Promise<boolean>; cancel: () => void }>());
  const mounted = useRef(true);
  const callbacks = useRef({ activate, isCurrentBoard, onUnavailable, onBusyChange });
  callbacks.current = { activate, isCurrentBoard, onUnavailable, onBusyChange };
  useEffect(() => {
    mounted.current = true;
    const requests = pending.current;
    return () => {
      mounted.current = false;
      for (const request of requests.values()) request.cancel();
    };
  }, []);
  useEffect(() => {
    for (const [boardId, request] of pending.current) {
      if (!isCurrentBoard(boardId)) request.cancel();
    }
  });

  return useCallback((boardId: string): Promise<boolean> => {
    for (const [id, request] of pending.current) {
      if (!callbacks.current.isCurrentBoard(id)) request.cancel();
    }
    const existing = pending.current.get(boardId);
    if (existing) return existing.promise;
    const operation = callbacks.current.activate;
    let cancelled = false;
    let busy = false;
    let cancelDelay: (() => void) | undefined;
    const isActive = () => !cancelled && mounted.current && callbacks.current.isCurrentBoard(boardId);
    const clearBusy = () => {
      if (busy) {
        busy = false;
        callbacks.current.onBusyChange?.(boardId, false);
      }
    };
    const cancel = () => {
      cancelled = true;
      cancelDelay?.();
      clearBusy();
    };
    const request = Promise.resolve().then(async () => {
      for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
        try {
          await operation(boardId);
          return isActive();
        } catch (error) {
          if (!isActive()) return false;
          if (!isRetryableBusy(error)) {
            callbacks.current.onUnavailable();
            return false;
          }
          if (attempt === MAX_ATTEMPTS) {
            callbacks.current.onUnavailable('busy');
            return false;
          }
          if (!busy) {
            busy = true;
            callbacks.current.onBusyChange?.(boardId, true);
          }
          await new Promise<void>((resolve) => {
            const timer = setTimeout(() => {
              cancelDelay = undefined;
              resolve();
            }, BUSY_RETRY_DELAY_MS);
            cancelDelay = () => {
              clearTimeout(timer);
              cancelDelay = undefined;
              resolve();
            };
          });
          if (!isActive()) return false;
        }
      }
      return false;
    }).finally(() => {
      clearBusy();
      pending.current.delete(boardId);
    });
    pending.current.set(boardId, { promise: request, cancel });
    return request;
  }, []);
}
